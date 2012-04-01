#!/usr/bin/python

# ***************************************************************************
# Process: a better subprocess handler--with a timeout and no sigalrm
#
#   Runs a process as a child, watches the stdout and/or stderr streams for
#   data, and saves it into a buffer as it comes it.  It will stop the progam
#   if it runs over a timeout, sending it increasingly stern signals (INT,
#   TERM, then as many KILLS as it takes).  Doesn't use alarm signals to
#   execute the timeout, so this should be thread safe (*not tested yet*)!
#
# ***************************************************************************

import os
import time
import shlex
import fcntl
import errno
import select
import signal
import os.path
import traceback

from subprocess import Popen, PIPE, STDOUT

__all__ = ['ProcessError', 'CannotKill', 'TimedOut', 'PIPE', 'STDOUT',
           'Program', 'Process', 'TimeoutProcess', 'CallbackProcess',
           'run_with_timeout']


class ProcessError(Exception):
    pass

class CannotKill(ProcessError):
    pass

class TimedOut(ProcessError):
    pass

class CallbackFailed(ProcessError):
    pass


class Program(object):
    """ Object to parse a command line and give the proper args.  Will
        update the environment with new @env if present and @update_env is
        True, if @env present and @update_env if False, will run the new
        program in those environment variables only.  If @env is None
        (the default), the environment is passed through untouched.
    """
    def __init__(self, cmdline, env=None, update_env=True, strict=False):
        self.args = shlex.split(cmdline)
        if env and update_env:
            new_env = dict(os.environ)
            new_env.update(env)
            self.env = new_env
        else:
            self.env = env
        self.cmdline = cmdline
        self.exe = self.args[0]
        if strict:
            self._strict_checks()

    def _strict_checks(self):
        if not os.path.exists(self.exe):
            raise ProcessError("Cannot find executable %s" % self.exe)
        if not os.access(self.exe, os.X_OK):
            raise ProcessError("Permission denied to exec %s" % self.exe)

    def __str__(self):
        return str(self.cmdline)


class Process(object):
    """ An object that runs a subprocess gathering output into strings.
        Takes a Program object as @prog and stdio streams are by default
        output to a buffer.
    """
    def __init__(self, prog, stdout=PIPE, stderr=PIPE, stdin=None):
        self.cmd = prog
        self.exit_status = None
        self.outstream = stdout
        self.errstream = stderr
        self.instream = stdin

    def start(self, close_fds=True):
        """ Forks and execs the child process, then returns.  Child write()'s
            will block if out/err is PIPE and it fills up without being read.
        """

        proc = Popen(self.cmd.args, stdout=self.outstream, stderr=self.errstream,
                     stdin=self.instream, env=self.cmd.env, close_fds=close_fds)
        self.pid = proc.pid

        # Make opened pipes non-blocking so read() below works alright
        if self.outstream == PIPE:
            flags = fcntl.fcntl(proc.stdout, fcntl.F_GETFL)
            fcntl.fcntl(proc.stdout, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        if self.errstream == PIPE:
            flags = fcntl.fcntl(proc.stderr, fcntl.F_GETFL)
            fcntl.fcntl(proc.stderr, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self.proc = proc

    def gather_output(self, poll_freq=0.1):
        """ Talks to a process, using select to multiplex over the various
            input/output pipes created by Popen, and checks for a timeout
            every poll_freq second. Will block till program ends or timeout.
        """
        self.start()
        self.stdout = ''
        self.stderr = ''

        read_fds = [x for x in (self.proc.stderr, self.proc.stdout) if x is not None]
        if self.proc.stdin:
            write_fds = [self.proc.stdin]
        else:
            write_fds = []

        while True:
            try:
                rd, wr, er = select.select(read_fds, write_fds, [], poll_freq)
            except select.error, e:
                if e.args[0] == errno.EINTR:
                    continue
                raise
            for x in rd:
                if x is self.proc.stdout:
                    self.stdout += x.read()
                elif x is self.proc.stderr:
                    self.stderr += x.read()
                else:
                    print "BUG: select should only return stdout/err"
            if self.chk_term():
                self.stdout += self.proc.stdout.read()
                self.stderr += self.proc.stderr.read()
                break

            if not self._periodic_status_checks():
                break

    def _periodic_status_checks(self):
        return True

    def run(self, poll_interval=0.5):
        """ Runs the program, not gathering output, so please don't use if
            you set PIPE for any output stream, because program write() calls
            may block if it fills up.
        """
        self.start()
        while True:
            time.sleep(poll_interval)
            if self.chk_term():
                break
            if not self._periodic_status_checks():
                break

    def kill_process(self, termpause=0.2, killpause=0.1, kill_lim=20):
        """ Send a sigterm, pause 'termpause' number of seconds, then if still
            running, send up to kill_lim sigkill's pausing 'killpause' between
            each one until either the program exists or raises CannotKill()
        """
        if self.chk_term():
            return
        time.sleep(termpause)
        if self.chk_term():
            return
        self.send_signal(signal.SIGTERM)
        time.sleep(killpause)
        if self.chk_term():
            return
        ctr = 0
        while not self.chk_term():
            time.sleep(killpause)
            self.send_signal(signal.SIGKILL)
            ctr += 1
            if ctr > kill_lim:
                raise CannotKill("PID: %d not responding to sigkill" % self.pid)

    def chk_term(self):
        """ Set exit_status and return True if terminated, or return False """
        if self.proc.poll() is not None:
            self.exit_status = self.proc.returncode
            return True
        return False

    def send_signal(self, signum):
        """ Send a signal to the process, returning True if it was delivered,
            False if the process has exited, and raising an exception on any
            other error.
        """
        try:
            os.kill(self.pid, signum)
        except OSError, e:
            if e.errno != errno.ESRCH:
                raise
            return False
        return True

    def set_stderr(self, stream):
        self.errstream = stream

    def set_stdout(self, stream):
        self.outstream = stream


class TimeoutProcess(Process):
    """ Run a process with a timeout...each time around the select/wait loop
        check that the process didn't timeout
    """

    def __init__(self, prog, stdout=PIPE, stderr=PIPE, stdin=None,
                 timeout=None, raise_on_timeout=False):
        super(TimeoutProcess, self).__init__(prog, stdout, stderr, stdin)
        self.timed_out = False
        self.timeout = timeout
        self.raise_on_timeout = raise_on_timeout
        self.start_t = 0

    def post_checks(self):
        if self.timed_out:
            self.do_timeout()

    def start(self, close_fds=True):
        super(TimeoutProcess, self).start(close_fds)
        self.start_t = time.time()

    def do_timeout(self):
        self.send_signal(signal.SIGTERM)
        if self.proc.poll() is None:
            self.kill_process()
        if self.raise_on_timeout:
            raise TimedOut("PID: %d timed out" % self.pid)

    def _periodic_status_checks(self):
        if self.timeout and time.time() - self.start_t > self.timeout:
            self.timed_out = True
            return False
        return True

    def gather_output(self, poll_freq=0.1):
        super(TimeoutProcess, self).gather_output(poll_freq)
        self.post_checks()

    def run(self, poll_interval=0.5):
        super(TimeoutProcess, self).run(poll_interval)
        self.post_checks()


class CallbackProcess(TimeoutProcess):
    """ A process that runs a callback function periodically that, if it
        fails, terminates the process.  The extra arguments are as follows:
            @callback: callable object that runs periodically
            @callback_args: tuple of arguments to pass to callback function
            @callback_freq: approximatly how often to run the callback
        all other options are the same as for Process.

        The callback function API is really simple: return boolean true on
        success, boolean false on failure.  Stop execution of child program
        on the first failure.
    """

    def __init__(self, prog, callback, callback_args, callback_freq,
                 stdout=PIPE, stderr=PIPE, stdin=None, timeout=None,
                 raise_on_timeout=False, raise_on_callback=False):
        super(CallbackProcess, self).__init__(prog, stdout, stderr, stdin,
                                              timeout, raise_on_timeout)
        self.callback = callback
        self.callback_args = callback_args
        self.callback_freq = callback_freq
        self.callback_failure = False
        self.last_callback = 0
        self.raise_on_callback = raise_on_callback

    def _periodic_status_checks(self):
        now = time.time()
        if self.timeout and now - self.start_t > self.timeout:
            self.timed_out = True
            return False

        if now - self.last_callback >= self.callback_freq:
            try:
                success = self.callback(*self.callback_args)
            except Exception:
                traceback.print_exc()
                success = False

            if not success:
                self.callback_failure = True
                self.kill_process()
                return False
            self.last_callback = now

        return True

    def post_checks(self):
        if self.timed_out:
            self.do_timeout()
        if self.callback_failure and self.raise_on_callback:
            raise CallbackFailed("Callback failed for process %d" % self.pid)


def run_with_timeout(cmdline, env=None, timeout=None):
    """ Helper function, runs a command cmdline in environment env, gathers
        output, and stops execution forcibly if timeout is reached.  Returns
        the gathered data in a tuple of (out, err, status) and status is None
        if the program timed out
    """
    p = Program(cmdline, env)

    proc = Process(p, timeout=timeout)
    proc.gather_output()

    out = str(proc.stdout)
    err = str(proc.stderr)
    rv = int(proc.exit_status)

    del proc

    return out, err, rv


if __name__ == "__main__":

    def cb(a, b):
        if not hasattr(cb, 'a'):
            cb.a = a
        else:
            cb.a = cb.a + b
        if cb.a > 10:
            return False
        return True

    c = Program('find /', strict=True)
    p = CallbackProcess(c, cb, (1, 3), 0.1, timeout=0.7, raise_on_callback=0)
    p.gather_output(0.01)

    print "Stdout: %d bytes\nStderr: %d bytes" % (len(p.stdout), len(p.stderr))
    print "Return Value: %s" % p.exit_status
    print "Callback Failure: %s\nTimed Out: %s" % (p.callback_failure, p.timed_out)
