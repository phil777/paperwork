import heapq
import logging
import itertools
import threading
import traceback
import time

"""
Job scheduling

A major issue in Paperwork are non-thread-safe dependencies (for instance,
libpoppler). This is solved by having only one thread other than the Gtk
main-loop thread. It is the job scheduler. Any long action is run in this
thread to avoid blocking the GUI.
"""

logger = logging.getLogger(__name__)


class JobException(Exception):
    def __init__(self, reason):
        Exception.__init__(self, reason)


class JobFactory(object):
    def __init__(self, name):
        self.name = name
        self.id_generator = itertools.counter()

    def make(self, **kwargs, *args):
        """Child class must override this method"""
        raise NotImplementedError()

    def __eq__(self, other):
        return self is other


class Job(object):
    MAX_TIME_FOR_UNSTOPPABLE_JOB = 0.5  # secs

    # some jobs can be interrupted. In that case, the job should store in
    # the instance where it stopped, so it can resume its work when do()
    # is called again.
    # If can_stop = False, the job should never last more than
    # MAX_TIME_FOR_UNSTOPPABLE_JOB
    can_stop = False

    priority = 0 # the higher priority is run first

    def __init__(self, job_factory, job_id):
        self.factory = job_factory
        self.id = job_id

    def do(self):
        """Child class must override this method"""
        raise NotImplementedError()

    def stop(self):
        """
        Only called if can_stop == True.
        Child class must override this method if can_stop == True.
        This function is run from the Gtk thread. It must *not* block
        """
        raise NotImplementedError()

    def __cmp__(self, other):
        # yes, this is reversed, and this is wanted:
        # we want the higher priority first
        return cmp(other.priority, self.priority)

    def __eq__(self, other):
        return self is other


class JobScheduler(object):
    def __init__(self, name):
        self.name = name
        self._thread = None
        self.running = False

        # _job_queue_cond.acquire()/release() protect the job queue
        # _job_queue_cond.notify_all() is called each time the queue is modified
        #  (except on cancel())
        self._job_queue_cond = threading.Condition()
        self._job_queue = []
        self._active_job = None

    def start(self):
        """Starts the scheduler"""
        assert(not self.running)
        assert(self._thread is None)
        logger.info("[Scheduler %s] Starting" % self.name)
        self._thread = Threading(target=self._run)
        self.running = True
        self._thread.start()

    def _run(self):
        logger.info("[Scheduler %s] Started" % self.name)

        while self.running:

            self._job_queue_cond.acquire()
            try:
                while len(self._job_queue) <= 0:
                    self._new_job_cond.wait()
                    if not self.running:
                        return

                self._active_job = heapq.heappop(self._job_queue)
            finally:
                self._job_queue_cond.release()

            if not self.running:
                return

            # we are the only thread changing self._active_job,
            # so we can safely use it even if we didn't keep the lock
            # on self._job_queue_lock

            assert(self._active_job is not None)

            try:
                self._active_job.do()
            except Exception, exc:
                logger.warning("Job %s:%d raised an exception: %s"
                               % (self._active_job.factory.name,
                                  self._active_job.id,
                                  str(exc)))
                traceback.print_exc()

            self._job_queue_cond.acquire()
            try:
                self._active_job = None
                self._job_queue_cond.notify_all()
            finally:
                self._job_queue_cond.release()

            if not self.running:
                return


    def add(self, job):
        logger.debug("[Scheduler %s] Queuing job %s:%d"
                     % (self.name, job.factory.name, job.id))

        self._job_queue_cond.acquire()
        try:
            if (job in self._job_queue or job == self._active_job):
                self._job_queue_cond.release()
                raise JobException("Job %s:%d already scheduled"
                                   % (job.factory.name, job.id))
            heapq.push(self._job_queue, job)
            self._job_queue_cond.notify_all()
        finally:
            self._job_queue_cond.release()


    def cancel_matching_jobs(self, condition):
        self._job_queue_cond.acquire()
        try:
            try:
                for job in self._job_queue:
                    if condition(job.factory):
                        self._job_queue.remove(job)
                        logger.debug("[Scheduler %s] Job %s:%d cancelled"
                                     % (job.factory.name, job.id))
            except ValueError:
                pass

            if condition(self._active_job):
                if job.can_stop:
                    job.stop()
                else:
                    logger.warning(
                        "[Scheduler %s] Tried to stop job %s:%d, but it can't"
                        " be stopped" % (job.factory.name, job.id))
                self._job_queue_cond.wait()
                logger.debug("[Scheduler %s] Job %s:%d halted"
                             % (job.factory.name, job.id))
        finally:
            self._job_queue_cond.release()

    def cancel(self, target_job):
        logger.debug("[Scheduler %s] Canceling job %s:%d"
                     % (job.factory.name, job.id))
        self.cancel_matching_jobs(
            lambda job: (job == target_job))

    def cancel_all(self, factory):
        logger.debug("[Scheduler %s] Canceling all jobs %s"
                     % (factory.name))
        self.cancel_matching_jobs(
            lambda job: (job.factory == factory))

    def stop(self):
        assert(self.running)
        assert(self._thread is not None)
        logger.info("[Scheduler %s] Stopping" % self.name)

        self.running = False

        self._job_queue_cond.acquire()
        try:
            if self._active_job is not None:
                self._active_job.stop()
            self._job_queue_cond.notify_all()
        finally:
            self._job_queue_cond.release()

        self._thread.join()
        self._thread = None

        logger.info("[Scheduler %s] Stopped" % self.name)