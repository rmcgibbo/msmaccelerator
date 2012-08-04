import sys, os
import numpy as np
import logging
import numbers
from collections import defaultdict

# msmbuilder imports
from msmbuilder.MSMLib import GetCountMatrixFromAssignments
from msmbuilder.MSMLib import EstimateReversibleCountMatrix
from msmbuilder.MSMLib import ErgodicTrim, ApplyMappingToAssignments
from msmbuilder import clustering, metrics
import msmbuilder.Trajectory
from msmbuilder.assigning import assign_in_memory

from sqlalchemy.sql import and_, or_
from msmaccelerator import Project
from models import Trajectory, Forcefield, MarkovModel, MSMGroup
from database import Session
import sampling
from utils import load_file, save_file

logger = logging.getLogger('MSMAccelerator.Builder')

def n_rounds():
    return Session.query(MSMGroup).count()
        
def is_sufficient_new_data():
    """Is there sufficient new data to build a new round?
        
    Returns
    -------
    truth : boolean
        True if there is sufficient new data for a new round
    """

    qg, qt = Session.query(MSMGroup), Session.query(Trajectory)
        
    msmgroup = qg.order_by(MSMGroup.id.desc()).first()
    if msmgroup is not None:
        n_built = qt.filter(Trajectory.msm_groups.contains(msmgroup)).count()
    else:
        n_built = 0
        
    n_total = qt.filter(Trajectory.returned_time != None).count()
        
    truth = n_total >= n_built + Project().num_trajs_sufficient_for_round
        
    logger.info("%d trajs total, %d trajs built. Sufficient? %s", n_total, n_built, truth)
    return truth
    
    
def run_round(checkdata=True):
    """Activate the builder and build new MSMs (if necessary)
    
    First, check to see if there is enough data are to warrant building a
    new set of MSMs. Assuming yes, do a joint clustering over all of the
    data, and then build MSMs for each forcefield on that state space.
    
    Parameters
    ----------
    checkdata : boolean, optional
         If False, skip the checking process
    
    Returns
    -------
    happened : boolean
        True if we actually did a round of MSM building, False otherwise
    """
        
    if checkdata:
        logger.info("Checking if sufficient data has been acquired.")
        if not is_sufficient_new_data():
            return False
    else:
        logger.info("Skipping check for adequate data.")
        
    # use all the data together to get the cluster centers
    generators, db_trajs = joint_clustering()
        
    msmgroup = MSMGroup(trajectories=db_trajs)
    for ff in Session.query(Forcefield).all():
        trajs = filter(lambda t: t.forcefield == ff, db_trajs)
        msm = build_msm(ff, generators=generators, trajs=trajs)
        msmgroup.markov_models.append(msm)
        
    # add generators to msmgroup
    Session.add(msmgroup)
    Session.flush()
    msmgroup.populate_default_filenames()
        
    msmgroup.trajectories = db_trajs
    msmgroup.n_states = len(generators)
    save_file(msmgroup.generators_fn, generators)

        
    for msm in msmgroup.markov_models:
        msm.populate_default_filenames()
        if hasattr(msm, 'counts'):
            save_file(msm.counts_fn, msm.counts)
        if hasattr(msm, 'assignments'):
            save_file(msm.assignments_fn, msm.assignments)
        if hasattr(msm, 'distances'):
            save_file(msm.distances_fn, msm.distances)
            save_file(msm.inverse_assignments_fn, invert_assignments(msm.assignments))
        

    # ======================================================================#
    # HERE IS WHERE THE ADAPTIVE SAMPLING ALGORITHMS GET CALLED
    # The obligation of the adaptive_sampling routine is to set the
    # model_selection_weight on each MSM/forcefield and the microstate
    # selection weights
    # check to make sure that the right fields were populated
    try:
        Project().adaptive_sampling(Session, msmgroup)
        
        for msm in msmgroup.markov_models:
            if not isinstance(msm.model_selection_weight, numbers.Number):
                raise ValueError('model selection weight on %s not set correctly' % msm)
            if not isinstance(msm.microstate_selection_weights, np.ndarray):
                raise ValueError('microstate_selection_weights on %s not set correctly' % msm)
    except Exception as e:
        logging.error('ADAPTIVE SAMPLING ERROR')
        logging.error(e)
        sampling.default(Session, msmgroup)
        
    #=======================================================================#

        
    Session.flush()       
    logger.info("Round completed sucessfully")
    return True
        
        
def joint_clustering():
    """Jointly cluster the the data from all of the forcefields
    
    Returns
    -------
    generators : msmbuilder.Trajectory
    """
    logger.info('Running joint clustering')
    
    # load up all the trajs in the database
    db_trajs = Session.query(Trajectory).filter(Trajectory.returned_time != None).all()
    if len(db_trajs) == 0:
        raise RuntimeError()
        
    # load the xyz coordinates from disk for each trajectory
    load = lambda v: msmbuilder.Trajectory.LoadTrajectoryFile(v)
    loaded_trjs = [load(t.lh5_fn)[::Project().stride] for t in db_trajs]
    
    clusterer = Project().clusterer(trajectories=loaded_trjs)
    return clusterer.get_generators_as_traj(), db_trajs

            
def build_msm(forcefield, generators, trajs):
    """Build an MSM for this forcefield using the most recent trajectories
    in the database.
        
    If supplied, use the supplied generators
        
    Parameters
    ----------
    forcefield : models.Forcefield
        database entry on the forcefield that we build for
    generators : msmbuilder.Trajectory
    trajs : list of models.Trajectory

    Returns
    -------
    msm : models.MarkovModel
    
    """
        
    # I want to use assign_in_memory, which requires an msmbuilder.Project
    # so, lets spoof it
        
        
    if len(trajs) == 0:
        return MarkovModel(forcefield=forcefield)
        
    class BuilderProject(dict):
        def __init__(self):
            self['NumTrajs'] = len(trajs)
            self['TrajLengths'] = np.array([t.length for t in trajs])
            
        def LoadTraj(self, trj_index):
            if trj_index < 0 or trj_index > len(trajs):
                raise IndexError('Sorry')
            return msmbuilder.Trajectory.LoadTrajectoryFile(trajs[trj_index].lh5_fn)
        
        
    logger.info('Assigning...')
    assignments, distances = assign_in_memory(Project().metric, generators, BuilderProject())
        
    logger.info('Getting counts...')
    counts = construct_counts_matrix(assignments)
        
        
    model = MarkovModel(forcefield=forcefield, trajectories=trajs)
    model.counts = counts
    model.assignments = assignments
    model.distances = distances
        
    return model
        
        
def construct_counts_matrix(assignments):
    """Build and return a counts matrix from assignments. Symmetrize either
    with transpose or MLE based on the value of the self.symmetrize variable
        
    Also modifies the assignments file that you pass it to reflect ergodic
    trimming"""
        
    n_states  = np.max(assignments.flatten()) + 1
    raw_counts = GetCountMatrixFromAssignments(assignments, n_states,
                                               LagTime=Project().lagtime, Slide=True)
        
    ergodic_counts = None
    if Project().trim:
        raise NotImplementedError(('Trimming is not yet supported because '
                                   'we need to keep track of the mapping from trimmed to '
                                   ' untrimmed states for joint clustering to be right'))
        try:
            ergodic_counts, mapping = ErgodicTrim(raw_counts)
            ApplyMappingToAssignments(assignments, mapping)
            counts = ergodic_counts
        except Exception as e:
            logger.warning("ErgodicTrim failed with message '{0}'".format(e))

    else:
        logger.info("Ignoring ergodic trimming")
        counts = raw_counts
        
    if Project().symmetrize == 'transpose':
        logger.debug('Transpose symmetrizing')
        counts = counts + counts.T
    elif Project().symmetrize == 'mle':
        logger.debug('MLE symmetrizing')
        counts = EstimateReversibleCountMatrix(counts)
    elif Project().symmetrize == 'none' or (not Project().symmetrize):
        logger.debug('Skipping symmetrization')
    else:
        raise ValueError("Could not understand symmetrization method: %s" % Project().symmetrize)
        
    return counts


# UTILITY FUNCTION
# THIS SHOULD REALLY BE INSIDE MSMBUILDER
def invert_assignments(assignments):
    inverse_assignments = defaultdict(lambda:  [])
    for i in xrange(assignments.shape[0]):
        for j in xrange(assignments.shape[1]):
            if assignments[i,j] != -1:
                inverse_assignments[assignments[i,j]].append((i,j))
    for key, value in inverse_assignments.items():
        inverse_assignments[key] = np.array(value)
        
    return dict(inverse_assignments)
