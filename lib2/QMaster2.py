from work_queue import Task, WorkQueue, set_debug_flag
from work_queue import WORK_QUEUE_SCHEDULE_FCFS
from work_queue import WORK_QUEUE_SCHEDULE_FILES
import time
from glob import glob
import yaml
import os, sys
import shutil
import numpy as np
import threading
import cPickle as pickle
from Queue import LifoQueue
import subprocess
import logging

from msmbuilder import Trajectory

#set_debug_flag('debug')
#set_debug_flag('wq')

class QMaster(threading.Thread):
    def __init__(self, project, port, log_freq=600): # 600 seconds
        """Initialize the QMaster
        
        Parameters
        ----------
        project : 
        port : int
        log_freq : int, optional
            frequency to print info about the status of the work queue.
            In units of seconds. Default is to print every 10 minutes.
        """
        
        threading.Thread.__init__(self)
        self.project = project
        self.db = self.project.db
        
        self.log_freq = log_freq  # print time in seconds
        self.wake_freq = 1 # seconds
        
        self.wq = WorkQueue(port, name='MSMAccelerator', catalog=True, exclusive=False)
        self.logger = logging.getLogger('MSMAccelerator.QMaster')
        self.logger.info('WORK QUEUE MASTER LISTENING ON PORT: {0}'.format(self.wq.port))
        self.logger.info('(Start a local worker with >> work_queue_worker -d all localhost {0} & )'.format(self.wq.port))
        
        # method controls whether or not we need to bring back solvated_xtc as well
        if self.project.method == 'explicit':
            self.return_wet_xtc = True
        elif self.project.method == 'implicit':
            self.return_wet_xtc = False
        else:
            raise Exception("project.method must be 'explicit' or 'implicit'")
        self.logger.info('Return wet xtc set to %s', self.return_wet_xtc)
        
        # what does this specify algorithm do?
        self.wq.specify_algorithm(WORK_QUEUE_SCHEDULE_FCFS)
        
        # fast abort kills jobs that appear to be stragling (taking more than 1.5x average)
        #self.wq.activate_fast_abort(1.5)
        
        # setting the stop event signals for the thread to die
        self._stop = threading.Event()
        
        # the thread sets the  event every time a job returns or there are no waiting jobs
        # and it finished post processing. See the wait method
        self._mainloop_wake_event_cause = None
        self._mainloop_wake_event = threading.Event()
        
        # start the thread
        self.start()
    
    def run(self):
        """Main thread-loop for the QMaster thread"""
        last_print = time.time()
        
        while True:
            time.sleep(self.wake_freq)
            
            if not self.wq.empty():
                t = self.wq.wait(self.wake_freq)
                if t:
                    if t.return_status != 0:
                        self.logger.error('Worker returned nonzero exit status for job: {0}'.format(str(job_dict)))
                    else:
                        self.on_return(t)
                    self._mainloop_wake_event_cause = 'job returned'
                    self._mainloop_wake_event.set()
            
            if self.wq.stats.tasks_waiting == 0 and not self._mainloop_wake_event.is_set():
                self._mainloop_wake_event_cause = 'queue empty'
                self._mainloop_wake_event.set() # also set the event if there are no tasks in the queue

            if self._stop.is_set():
                self.logger.info('Recieved stop signal. Shutting down all workers')
                self.wq.shutdown_workers(0) # 0 indicates to shut all of them down
                sys.exit(0)
            
            if time.time() - last_print > self.log_freq:
                self.logger.info('workers initialized: %d, ready: %d, busy: %d' % (self.wq.stats.workers_init, self.wq.stats.workers_ready, self.wq.stats.workers_busy))
                self.logger.info('workers running: %d, waiting: %d, complete: %d' % (self.wq.stats.tasks_running, self.wq.stats.tasks_waiting, self.wq.stats.tasks_complete))
                last_print = time.time()

    def num_jobs_waiting(self):
        """Number of jobs waiting to be sent out
        
        This number should be kept at 1, and when it drops to zero a new job
        should be generated.
        
        Returns
        -------
        n : int
            The number
        """
        return self.wq.stats.tasks_waiting

    def num_jobs_in_queue(self):
        """Get the number of jobs currently in the work queue
        
        This includes both the jobs running remotely and the ones waiting
        here
        
        Returns
        -------
        n : int
            The number
        """
        return self.wq.stats.tasks_running + self.wq.stats.tasks_waiting
        
    def stop(self):
        """Signal the Qmaster thread to stop"""
        self._stop.set()

    def wait(self):
        """Block until some sort of action happens in the main-thread loop.
        
        This call will return either when a job as returned from the workers,
        or when the queue is empty (last job in the local queue has been sent
        out)
        
        Returns
        -------
        wakeup_cause : str
            Either 'job returned' or 'queue empty', depending on the reason
        """
        self._mainloop_wake_event.wait()
        self._mainloop_wake_event.clear()
        cause = self._mainloop_wake_event_cause
        if not cause in ['job returned', 'queue empty']:
            raise Exception('Bad wakeup cause')
        return cause
    
    def submit(self, traj):
        """ Submit a job to the work-queue for further sampling.
        
        Parameters
        ----------
        traj : models.Trajectory
        """
        
        remote_driver_fn = os.path.split(traj.forcefield.driver)[1]
        remote_pdb_fn = os.path.split(traj._init_pdb_fn)[1]
        remote_output_fn = 'production_dry{}'.format(traj.forcefield.output_extension)
        
        task = Task('python ./{driver} {pdb_fn} {ff} {water} {mode} {threads} > {wqlog}'.format(
            pdb_fn=remote_pdb_fn,
            mode=traj.mode,
            driver=remote_driver_fn,
            ff=traj.forcefield.name,
            water=traj.forcefield.water,
            wqlog = traj._wqlog_fn,
            threads=traj.forcefield.threads))
        
        traj.submit_time = time.time()
        
        
        task.specify_input_file(str(traj.forcefield.driver), str(remote_driver_fn))
        task.specify_output_file(str(traj._wqlog_fn), 'logs/driver.log')
        task.specify_input_file(str(traj._init_pdb_fn), str(remote_pdb_fn))
        # this is the xtc without water
        task.specify_output_file(traj._dry_xtc_fn, remote_output_fn)
        
        if self.return_wet_xtc:
            # this is the XTC file with waters, generated by the driver
            # when you're doing implicit solvent only, this stuff is not used.
            remote_output_fn = 'production_wet{}'.format(traj.forcefield.output_extension)
            task.specify_output_file(str(trj._wet_xtc_fn), remote_output_fn)
            task.specify_output_file(str(trj._last_wet_snapshot_fn), 'last_wet_snapshot.pdb')
        else:
            self.logger.debug('Not requesting production_wet{} from driver (implicit)'.format(traj.forcefield.output_extension))
        
        task.specify_tag(traj.id)
        task.specify_algorithm(WORK_QUEUE_SCHEDULE_FILES) # what does this do?
        
        self.wq.submit(task)    
        self.logger.info('Submitted to queue: {0} ({1}, {2})'.format(fileroot, job['ff'], job['name']))
        
    def on_return(self, task):
        """Called by main thread on the return of data from the workers.
        Post-processing"""
        self.logger.info('Retrieved "{0}" xtc. Converting to lh5...'.format(job['name']))
        
        try:
            # save lh5 version of the trajectory
            traj_dir = self.project.traj_dir(job['ff'])
            trajnum = len(glob(os.path.join(traj_dir, '*.lh5')))
            lh5_fn = os.path.abspath(os.path.join(traj_dir, '%d.lh5' % trajnum))
            conf = Trajectory.LoadTrajectoryFile(self.project.pdb_topology_file)
            traj = Trajectory.LoadTrajectoryFile(job['dry_xtc'], Conf=conf)
            traj.SaveToLHDF(lh5_fn)
        
        except Exception as e:
            self.logger.error('When postprocessing {0}, convert to lh5 failed!'.format(str(job)))
            self.logger.exception(e)
            raise
        
        # create softlink to the lh5 trajectory in the JointFF directory
        softlink_dir = self.project.traj_dir(self.project.joint_ff['name'])
        
        softlink_num = len(glob(os.path.join(softlink_dir, '*.lh5')))
        softlink_fn = os.path.join(softlink_dir, '%d.lh5' % softlink_num)
        os.symlink(lh5_fn, softlink_fn)

        # update the TrajLog file
        job['AllFF_fn'] = softlink_fn
        job['lh5_fn'] = lh5_fn
        job['TrajLength'] = len(traj)
        job['lh5_trajnum'] = trajnum
        self.project.traj_log_add(job)
        self.logger.info('Finished converting new traj to lh5 sucessfully')
    
if __name__ == '__main__':
    q = QMaster('.')
    job = {'name': 'testjob3',
           'driver': 'python /home/rmcgibbo/monakos/drivers/GROMACS/gromacs_driver.py',
           'conf': Trajectory.LoadFromPDB('/home/rmcgibbo/monakos/drivers/GROMACS/ala5.pdb'),
           'ff': 'amber99sb-ildn',
           'water': 'tip3p',
           'mode': 'equilibration',
           'threads': 8}
    
    q.submit(job)
    q.stop()
