# Copyright (C) 2020 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import queue
import socket
import threading
import time

from cuckoo.common import machines
from cuckoo.common.config import cfg
from cuckoo.common.ipc import UnixSocketServer, ReaderWriter
from cuckoo.common.log import CuckooGlobalLogger
from cuckoo.common.storage import TaskPaths
from cuckoo.machineries import errors

class MachineryManagerError(Exception):
    pass

class MachineDoesNotExistError(MachineryManagerError):
    pass

log = CuckooGlobalLogger(__name__)

# These call the machines underlying machinery implementation of the called
# method. All of these cause the state to change. This change can take a while.
# We therefore return the expected state, a maximum waiting time, and a
# potential handler function that can be called if the timeout expires.
def stop(machine):
    """Stop the given machine"""
    try:
        machine.machinery.stop(machine)
    finally:
        try:
            stop_netcapture(machine)
        except errors.MachineNetCaptureError as e:
            log.error(e)

    return machines.States.POWEROFF, 60, None

def acpi_stop(machine):
    """Stop the machine using an ACPI signal. Normal stop is called when the
     timeout of 120 seconds expires.
    """
    try:
        machine.machinery.acpi_stop(machine)
    finally:
        try:
            stop_netcapture(machine)
        except errors.MachineNetCaptureError as e:
            log.error(e)
    # Call the stop function if stop takes longer than the timeout.
    return machines.States.POWEROFF, 120, stop

def restore_start(machine):
    """Restore the machine to its configured snapshot and start the machine"""
    try:
        start_netcapture(machine)
    except errors.MachineNetCaptureError as e:
        log.error(e)

    try:
        machine.machinery.restore_start(machine)
    except errors.MachineryError:
        try:
            stop_netcapture(machine)
        except errors.MachineNetCaptureError as e:
            log.error(e)
        raise

    return machines.States.RUNNING, 120, stop_netcapture

def norestore_start(machine):
    """Start the machine without restoring a snapshot"""
    try:
        start_netcapture(machine)
    except errors.MachineNetCaptureError as e:
        log.error(e)

    try:
        machine.machinery.norestore_start(machine)
    except errors.MachineryError:
        try:
            stop_netcapture(machine)
        except errors.MachineNetCaptureError as e:
            log.error(e)
        raise

    return machines.States.RUNNING, 60, stop_netcapture

def start_netcapture(machine):
    """Ask the machinery to start network capture for the given machine"""
    if not machine.locked_by:
        return

    ignore_ip_ports = [
        (cfg("cuckoo", "resultserver", "listen_ip"),
         cfg("cuckoo", "resultserver", "listen_port")),
        (machine.ip, 8000)
    ]
    machine.machinery.start_netcapture(
        machine, TaskPaths.pcap(machine.locked_by),
        ignore_ip_ports=ignore_ip_ports
    )

def stop_netcapture(machine):
    """Stop the network capture for a machine"""
    machine.machinery.stop_netcapture(machine)

def dump_memory(machine):
    """Create a memory dump for the given running machine in the task folder
    of the task that has currently locked the machine."""
    machine.machinery.dump_memory(
        machine, TaskPaths.memory_dump(machine.locked_by)
    )
    return machines.States.RUNNING, 60, None

def machine_state(machine):
    """Return a normalized machine state of the given machine."""
    return machine.machinery.state(machine)

def shutdown(machinery):
    """Shutdown all machines of the given machinery module"""
    machinery.shutdown()

class _MachineryMessages:

    @staticmethod
    def success():
        return {"success": True}

    @staticmethod
    def fail(reason=""):
        return {"success": False, "reason": reason}

    @staticmethod
    def invalid_msg(reason):
        return {
            "success": False,
            "reason": reason
        }

class MachineryWorker(threading.Thread):
    """Retrieves a WorkTracker from work_queue and runs it. Running
    it must cause the 'action' function to return an expected state,
    timeout in seconds, and an optional timeout action function that
    is called if the timeout expires.

    After the action function returns, the WorkerTracker instance is added to
    the state_waiters list. Before running a new work action, the
    MachineryWorker thread checks that state/progress of all entries
    in the state_waiters list.

    A machine is disabled if a timeout for a WorkTracker has been reached or
    an unexpected state is reached.
    Unexpected meaning an error state or unhandled state. Handled states are
    located in a machinery module's statemapping attribute.
    """

    def __init__(self, machines, nodectx, work_queue, state_waiters,
                 waiter_lock):
        super().__init__()

        self._machines = machines
        self._ctx = nodectx
        self._work_queue = work_queue
        self._state_waiters = state_waiters
        self._waiter_lock = waiter_lock
        self.do_run = True

    def stop(self):
        self.do_run = False

    def _disable_machine(self, machine, reason):
        self._machines.mark_disabled(machine, reason)
        self._ctx.node.infostream.disabled_machine(machine.name, reason)

    def handle_waiters(self):
        if not self._waiter_lock.acquire(timeout=0.1):
            return

        try:
            for work in self._state_waiters[:]:
                try:
                    state = machine_state(work.machine)
                except errors.MachineryUnhandledStateError as e:
                    err = "Unhandled machine state"
                    log.error(
                        f" {err}. Disabling machine.",
                        machine=work.machine.name, error=e
                    )
                    self._disable_machine(work.machine, f"{err}. {e}")
                    work.work_failed(reason=f"{err}. {e}")

                    # Remove this work tracker from state waiting work trackers
                    # as we disabled the machine
                    work.stop_wait()
                    continue

                if state == work.expected_state:
                    log.debug(
                        "Updating machine state.",
                        machine=work.machine.name, newstate=work.expected_state
                    )

                    self._machines.set_state(work.machine, work.expected_state)
                    work.work_success()
                elif state == machines.States.ERROR:
                    err = "Machinery returned error state for machine"
                    log.error(
                        f"{err}. Disabling machine.", machine=work.machine.name
                    )
                    self._disable_machine(work.machine, err)
                    work.work_failed(reason=err)

                elif not work.timeout_reached():
                    # Timeout not reached. Continue with other state waiters.
                    continue
                else:
                    if work.has_fallback():
                        work.fall_back()
                    else:
                        err = "Timeout reached while waiting for machine to " \
                              "reach expected state."
                        log.error(
                            f"{err}. Disabling machine.",
                            machine=work.machine.name,
                            expected_state=work.expected_state,
                            actual_state=state
                        )
                        self._disable_machine(work.machine, err)
                        work.work_failed(reason=err)

                work.stop_wait()
        finally:
            self._waiter_lock.release()

    def run(self):
        while self.do_run:
            self.handle_waiters()

            work = self._work_queue.get_work()
            if not work:
                time.sleep(1)
                continue

            try:
                work.run_work()

                if work.expects_state_change():
                    work.start_wait()
                else:
                    work.work_success()
            except NotImplementedError:
                err = "Machinery does not support work type."
                log.error(
                    err, machine=work.machine.name,
                    machinery=work.machine.machinery.name,
                    action=work.work_func
                )

                err += f" action={work.work_func}"
                work.work_failed(reason=err)

            except errors.MachineStateReachedError:
                # Machine already has the state the executed work should put
                # it in. Log as warning for now. These errors should only occur
                # when stopping a machine, not when starting.
                log.debug(
                    "Machine already has expected state on action start.",
                    machine=work.machine.name,
                    expected_state=work.expected_state,
                    action=work.work_func
                )
                work.work_success()

            except errors.MachineUnexpectedStateError as e:
                err = "Machine has unexpected state"
                log.error(
                    f"{err}. Disabling machine.", machine=work.machine.name,
                    machinery=work.machine.machinery.name,
                    error=e
                )
                self._disable_machine(work.machine, f"{err}. {e}")
                work.work_failed(reason=f"{err}. {e}")

            except errors.MachineryUnhandledStateError as e:
                err = "Machine has an unknown/unhandled state"
                log.error(
                    f"{err}. Disabling machine", machine=work.machine.name,
                    machinery=work.machine.machinery.name, error=e
                )
                self._disable_machine(work.machine, f"{err}. {e}")
                work.work_failed(reason=f"{err}. {e}")

            except errors.MachineryError as e:
                err = "Machinery module error"
                log.error(
                    f"{err}. Disabling machine.",
                    machine=work.machine.name,
                    machinery=work.machine.machinery.name, error=e
                )
                self._disable_machine(work.machine, f"{err}. {e}")
                work.work_failed(reason=f"{err}. {e}")

            except Exception as e:
                err = "Unhandled error in machinery module."
                log.exception(
                    err, machine=work.machine.name,
                    machinery=work.machine.machinery.name,
                    action=work.work_func, error=e
                )

                err += f" action={work.work_func}. error={e}"
                work.work_failed(reason=err)
                # TODO mark this fatal error somewhere, so that it is clear
                # this happened.
                raise

class WorkTracker:

    def __init__(self, func, machine, readerwriter, machinerymngr):
        self.work_func = func
        self.machine = machine
        self.readerwriter = readerwriter
        self.machinerymngr = machinerymngr

        # Set by return values of the executed work
        self.expected_state = ""
        self.timeout = 0
        self.fallback_func = None

        # Set when wait starts
        self.waited = 0
        self.since = 0

    def lock_work(self):
        """Lock the machine action lock, so that no other queued
        actions for this machine can run until this action has released
        the lock"""
        return self.machine.action_lock.acquire(blocking=False)

    def expects_state_change(self):
        if self.expected_state:
            return True
        return False

    def unlock_work(self):
        """Release the action lock so that other queued work can acquire it
        and run its work for the machine"""
        self.machine.action_lock.release()

    def work_failed(self, reason=""):
        self.unlock_work()
        self.machinerymngr.queue_response(
            self.readerwriter, _MachineryMessages.fail(reason=reason)
        )

    def work_success(self):
        self.unlock_work()
        self.machinerymngr.queue_response(
            self.readerwriter, _MachineryMessages.success()
        )

    def run_work(self):
        log.debug(
            "Starting work.", machine=self.machine.name,
            action=self.work_func
        )
        self.expected_state, self.timeout, self.fallback_func = self.work_func(
            self.machine
        )

    def has_fallback(self):
        return self.fallback_func is not None

    def requeue(self):
        """Creates a new entry in the msg handler work queue of the current
        work. Can be used if the work cannot be performed for some reason"""
        self.machinerymngr.work_queue.add_work(
            self.work_func, self.machine, self.readerwriter, self.machinerymngr
        )

    def fall_back(self):
        """Queue a work instance of the fallback function in the work queue"""
        self.machinerymngr.work_queue.add_work(
            self.fallback_func, self.machine, self.readerwriter,
            self.machinerymngr
        )

    def start_wait(self):
        """Adds this object to the MachineryManager instance state_waiters
        list."""
        self.since = time.monotonic()
        self.machinerymngr.state_waiters.append(self)

    def stop_wait(self):
        """Remove this object from the MachineryManager instance state_waiters
        list."""
        self.machinerymngr.state_waiters.remove(self)

    def timeout_reached(self):
        self.waited += time.monotonic() - self.since
        if self.waited >= self.timeout:
            return True
        return False


class _WorkQueue:

    def __init__(self):
        self._queue = []
        self._lock = threading.Lock()

    def add_work(self, work_action, machine, readerwriter, msghandler):
        log.debug(
            "Machine action request", machine=machine.name, action=work_action
        )
        self._queue.append(
            WorkTracker(work_action, machine, readerwriter, msghandler)
        )

    def get_work(self):
        """Only returns a work instance if the machine for this work is not
        currently being used in another work instance. Returns None if
        no work is available or can be performed.

        A return work instance MUST be released by calling its work_success
        or work_fail function."""
        ignore = []
        with self._lock:
            for work in self._queue[:]:
                if work.machine.name in ignore:
                    continue

                # If the machine is locked, ignore any other work for it
                # that may be in the queue to keep the execution order FIFO.
                if not work.lock_work():
                    ignore.append(work.machine.name)
                    continue

                self._queue.remove(work)
                return work

        return None

class MachineryManager(UnixSocketServer):
    """The Machinery manager accepts connections and reads message from a
    client. These messages must contain an 'action' and machine name
    for which to perform this action. The action is anything that performs
    an action on/using the specified machine.

    Supported actions are listed in 'machinery_worker_actions'.

    For each action, a WorkTracker is created. The trackers are queued in a
    work queue. Work is retrieved from the work queue and executed by
    MachineryWorker threads. The work_queue, state_waiters, and waiter_lock
    are shared amount all MachineryWorker threads.

    Each WorkTracker contains a ReaderWriter with an underlying socket
    that is connected to the client requesting the action. When an action
    has been executed and has either succeeded or fail, a response is queued
    in the MachineryManager responses queue by calling .work_success()
    or work_fail() on a WorkTracker.

    Starting the MachineryManager automatically starts NUM_MACHINERY_WORKERS
    of MachineryWorker threads.
    """

    machinery_worker_actions = {
        "restore_start": restore_start,
        "norestore_start": norestore_start,
        "stop": stop,
        "acpi_stop": acpi_stop
    }

    # TODO read number of MachineryWorkers from a configuration file?
    NUM_MACHINERY_WORKERS = 4

    def __init__(self, manager_sock_path, nodectx):
        super().__init__(manager_sock_path)

        self.ctx = nodectx
        self.machines = machines.MachinesList()

        # A name->instance mapping of all loaded machinery modules
        self._machineries = {}
        self.workers = []

        # These are passed to worker threads
        self.state_waiters = []
        self.waiter_lock = threading.Lock()

        self.work_queue = _WorkQueue()
        self.responses = queue.Queue()

    def load_machineries(self, machinery_classes, previous_machinelist):
        """Creates instances of each given machinery class, initializes the
        instances, and loads their machines from their configuration files.

        machinery_classes: a list of imported machinery classes
        previous_machinelist: (Optional) a MachineList returned by
        read_machines_dump. Used to restore machine states from previous runs
        to machines that are still used. See cuckoo.common.machines.Machine
        for information about what states are restored.
        """
        for machinery_class in machinery_classes:
            machine_conf = cfg(machinery_class.name, subpkg="machineries")

            log.debug(
                "Loading machinery and its machines.",
                machinery=machinery_class.name
            )
            try:
                machinery_class.verify_dependencies()
                machinery = machinery_class(machine_conf)
                machinery.init()
                machinery.load_machines()
            except errors.MachineryError as e:
                raise MachineryManagerError(
                    f"Loading of machinery module {machinery_class.name} "
                    f"failed. {e}"
                )

            for machine in machinery.list_machines():
                try:
                    existing = self.machines.get_by_name(machine.name)
                    raise MachineryManagerError(
                        f"Machine name {machine.name} of "
                        f"{machinery_class.name} "
                        f"is not unique. It already exists "
                        f"in {existing.machinery.name}"
                    )
                except KeyError:
                    pass

                # Load state information from a machine obj loaded from a
                # machinestates dump of a previous run.
                if previous_machinelist:
                    try:
                        machine.load_stored_states(
                            previous_machinelist.get_by_name(machine.name)
                        )
                    except KeyError:
                        pass

                self.machines.add_machine(machine)
                log.debug(
                    "Machinery loaded machine.",
                    machinery=machinery.name, machine=machine.name
                )

            self._machineries[machinery.name] = machinery

        log.info("Loaded analysis machines", amount=self.machines.count())

    def start(self):

        for _ in range(self.NUM_MACHINERY_WORKERS):
            worker = MachineryWorker(
                self.machines, self.ctx, self.work_queue, self.state_waiters,
                self.waiter_lock
            )
            self.workers.append(worker)
            worker.start()

        self.create_socket()
        self.start_accepting(select_timeout=1)

    def shutdown_all(self):
        """Shutdown the machines of all loaded machinery modules"""
        for name, module in self._machineries.items():
            log.info("Shutting down machinery.", machinery=name)
            shutdown(module)

    def stop(self):
        if not self.do_run and not self.workers:
            return

        super().stop()
        for worker in self.workers:
            worker.stop()

        self.cleanup()

    def handle_connection(self, sock, addr):
        self.track(sock, ReaderWriter(sock))

    def queue_response(self, readerwriter, response, close=False):
        self.responses.put((readerwriter, response, close))

    def timeout_action(self):
        while not self.responses.empty():
            try:
                readerwriter, response, close = self.responses.get(block=False)
            except queue.Empty:
                break

            try:
                readerwriter.send_json_message(response)
            except socket.error as e:
                log.debug("Failed to send response.", error=e)
                self.untrack(readerwriter.sock)
                continue

            if close:
                self.untrack(readerwriter.sock)

    def handle_message(self, sock, msg):
        action = msg.get("action")
        machine_name = msg.get("machine")

        readerwriter = self.socks_readers[sock]

        # If no action or machine is given, send error and cleanup sock
        if not action or not machine_name:
            self.queue_response(
                readerwriter,
                _MachineryMessages.invalid_msg(
                    reason="Missing action or machine name"
                ), close=True
            )
            return

        try:
            machine = self.machines.get_by_name(machine_name)
        except KeyError as e:
            # Send error if the machine does not exist.
            self.queue_response(
                readerwriter,
                _MachineryMessages.invalid_msg(reason=str(e))
            )
            return

        worker_action = self.machinery_worker_actions.get(action)

        # If the action does not exist, send error and cleanup sock
        if not worker_action:
            self.queue_response(
                readerwriter,
                _MachineryMessages.invalid_msg(reason="No such action"),
                close=True
            )
            return

        self.work_queue.add_work(worker_action, machine, readerwriter, self)
