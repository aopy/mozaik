"""
This is the nexus of workflow execution controll of *mozaik*.
"""
from datetime import datetime
import logging
logging.basicConfig(filename='mozaik.log', level=logging.DEBUG)
import os
import time
from mozaik.storage.queries import *
import mozaik
from .cli import parse_workflow_args
from .storage.datastore import PickledDataStore
from .tools.distribution_parametrization import (
    MozaikExtendedParameterSet,
    load_parameters
)
from .tools.misc import result_directory_name
# from pyNN.spiNNaker.extra_models import *
# from spynnaker8.extra_models import Izhikevich_cond

logger = logging.getLogger(__name__)


class Global:
    """global variable container currently only containing the root_directory variable that points to the root directory of the model specification"""
    root_directory = './'


class FancyFormatter(logging.Formatter):
    """
    A log formatter that colours and indents the log message depending on the level.
    """

    DEFAULT_INDENTS = {
        "CRITICAL": "",
        "ERROR": "",
        "WARNING": "",
        "HEADER": "",
        "INFO": "  ",
        "DEBUG": "    "
    }

    def __init__(self, fmt=None, datefmt=None, mpi_rank=None):
        logging.Formatter.__init__(self, fmt, datefmt)
        self._indents = FancyFormatter.DEFAULT_INDENTS
        if mpi_rank is None:
            self.prefix = ""
        else:
            self.prefix = "%-3d" % mpi_rank

    def format(self, record):
        s = logging.Formatter.format(self, record)
        if record.levelname == "HEADER":
            s = "=== %s ===" % s
        return self.prefix + self._indents[record.levelname] + s


def init_logging(
    filename, file_level=logging.INFO, console_level=logging.WARNING, mpi_rank=None
):
    if mpi_rank is None:
        mpi_fmt = ""
    else:
        mpi_fmt = "%3d " % mpi_rank
    logging.basicConfig(
        level=file_level,
        format=(
            "%%(asctime)s %s%%(name)-10s %%(levelname)-6s %%(message)s"
            " [%%(pathname)s:%%(lineno)d]"
        )
        % mpi_fmt,
        filename=filename,
        filemode="w"
    )
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(FancyFormatter("%(message)s", mpi_rank=mpi_rank))
    logging.getLogger("").addHandler(console)
    return console


def setup_logging():
    """
    This functions sets up logging.
    """
    if mozaik.mpi_comm:
        init_logging(
            Global.root_directory + "log",
            file_level=logging.INFO,
            console_level=logging.INFO,
            mpi_rank=mozaik.mpi_comm.rank
        )
    else:
        init_logging(
            Global.root_directory + "log",
            file_level=logging.INFO,
            console_level=logging.INFO
        )


def run_workflow(simulation_name, model_class, create_experiments):
    """
    This is the main function that executes a workflow. 
    
    It expects it gets the simulation, class of the model, and a function that will create_experiments.
    The create experiments function get a instance of a model as the only parameter and it is expected to return 
    a list of Experiment instances that should be executed over the model.
    
    The run workflow will automatically parse the command line to determine the simulator to be used and the path to the root parameter file. 
    It will also accept . (point) delimited path to parameteres in the configuration tree, and corresponding values. It will replace each such provided
    parameter's value with the provided one on the command line. 
    
    Parameters
    ----------
    simulation_name : str
                    The name of the simulation.
    
    model_class : class
                The class from which the model instance will be created from.
    
    create_experiments : func
                       The function that returns the list of experiments that will be executed on the model.
    
    Examples
    --------
    The intended syntax of the commandline is as follows (note that the simulation run name is the last argument):
    
    >>> python userscript simulator_name num_threads parameter_file_path modified_parameter_path_1 modified_parameter_value_1 ... modified_parameter_path_n modified_parameter_value_n simulation_run_name
    """
    (
        simulation_run_name,
        simulator_name,
        num_threads,
        parameters_url,
        modified_parameters,
    ) = parse_workflow_args()


    # First we load the parameters just to retrieve seeds. We will throw them away, because at this stage the PyNNDistribution values were not yet initialized correctly.
    parameters = load_parameters(parameters_url, modified_parameters)
    p = {}
    # if parameters.has_key('mozaik_seed') : p['mozaik_seed'] = parameters['mozaik_seed']
    # if parameters.has_key('pynn_seed') : p['pynn_seed'] = parameters['pynn_seed']

    if "mozaik_seed" in parameters:
        p["mozaik_seed"] = parameters["mozaik_seed"]
    if "pynn_seed" in parameters:
        p["pynn_seed"] = parameters["pynn_seed"]


    # now initialize mpi with the seeds
    # print("START MPI")
    mozaik.setup_mpi(**p)
    
    # Now really load parameters
    print("Loading parameters")
    parameters = load_parameters(parameters_url, modified_parameters)
    print("Finished loading parameters")

    # exec "import pyNN.nest as sim" in  globals(), locals()
    # exec "import pyNN.spiNNaker as sim" in globals(), locals()
    # exec("import spynnaker8 as sim" in globals(), locals())

    import pyNN.spiNNaker as sim
    # import spynnaker8 as sim
    globals()["sim"] = sim
    
    # Create results directory
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')

    ddir = result_directory_name(
        simulation_run_name, simulation_name, modified_parameters
    )

    if mozaik.mpi_comm and mozaik.mpi_comm.rank != 0:
        Global.root_directory = (
            parameters.results_dir + ddir + "/" + str(mozaik.mpi_comm.rank) + "/"
        )
        # mozaik.mpi_comm.barrier()
    else:
        Global.root_directory = parameters.results_dir + ddir + "/"

    os.makedirs(Global.root_directory)
    if mozaik.mpi_comm and mozaik.mpi_comm.rank == 0:
        # mozaik.mpi_comm.barrier()
        pass
    
    
    if mozaik.mpi_comm.rank == 0:
        # let's store the full and modified parameters, if we are the 0 rank process
        parameters.save(Global.root_directory + "parameters", expand_urls=True)
        import pickle

        f = open(Global.root_directory + "modified_parameters", "wb")
        pickle.dump(modified_parameters, f)
        f.close()

    setup_logging()
    
    model = model_class(sim, num_threads, parameters)

    if mozaik.mpi_comm.rank == 0:
        # let's store some basic info about the simulation run
        f = open(Global.root_directory + "info", "w")
        f.write(
            str(
                {
                    "model_class": str(model_class),
                    "model_docstring": model_class.__doc__,
                    "simulation_run_name": simulation_run_name,
                    "model_name": simulation_name,
                    "creation_data": datetime.now().strftime("%d/%m/%Y-%H:%M:%S")
                }
            )
        )
        f.close()


    #import cProfile
    #cProfile.run('run_experiments(model,create_experiments(model),parameters)','stats_new')

    data_store = run_experiments(model, create_experiments(model), parameters)

    if mozaik.mpi_comm.rank == 0:
        data_store.save()

    import resource
    print(
        "Final memory usage: %iMB"
        % (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024))
    )
    return (data_store, model)


def run_experiments(model,experiment_list,parameters,load_from=None):
    """
    This is function called by :func:.run_workflow that executes the experiments in the `experiment_list` over the model. 
    Alternatively, if load_from is specified it will load an existing simulation from the path specified in load_from.
    
    Parameters
    ----------
    
    model : Model
          The model to execute experiments on.
    
    experiment_list : list
          The list of experiments to execute.
    
    parameters : ParameterSet
               The parameters given to the simulation run.
          
    load_from : str
              If not None it will load the simulation from the specified directory.
              
    Returns
    -------
    
    data_store : DataStore
               The data store containing the recordings.
    """
    
    # first lets run all the measurements required by the experiments
    logger.info("Starting Experiemnts")
    if load_from == None:
        # print("load false")
        data_store = PickledDataStore(
            load=False,
            parameters=MozaikExtendedParameterSet(
                {
                    "root_directory": Global.root_directory,
                    "store_stimuli": parameters.store_stimuli
                }
            )
        )
    else:
        # print("load true")
        data_store = PickledDataStore(
            load=True,
            parameters=MozaikExtendedParameterSet(
                {"root_directory": load_from, "store_stimuli": parameters.store_stimuli}
            )
        )
    # print("controller data_store get_stored_esyn_ids 1",
    #      param_filter_query(data_store, sheet_name="V1_Exc_L4").get_segments()[0].get_stored_esyn_ids())

    # print("controller model.neuron_ids() ", model.neuron_ids())
    data_store.set_neuron_ids(model.neuron_ids())
    data_store.set_neuron_positions(model.neuron_positions())
    # print("controller model.neuron_annotations() ", model.neuron_annotations())
    data_store.set_neuron_annotations(model.neuron_annotations())
    # print("controller str(parameters) ", str(parameters))
    data_store.set_model_parameters(str(parameters))
    # print("controller str(model.sheet_parameters() ", str(model.sheet_parameters()))
    data_store.set_sheet_parameters(str(model.sheet_parameters()))
    # print("controller [(str(exp.__class__), str(exp.parameters)) for exp in experiment_list] ",
    #      [(str(exp.__class__), str(exp.parameters)) for exp in experiment_list])
    # print("controller data_store get_stored_esyn_ids 1",
    #      param_filter_query(data_store, sheet_name="V1_Exc_L4").get_segments()[0].get_stored_esyn_ids())
    data_store.set_experiment_parametrization_list(
        [(str(exp.__class__), str(exp.parameters)) for exp in experiment_list]
    )
    # print("controller data_store get_stored_esyn_ids 1",
    #      param_filter_query(data_store, sheet_name="V1_Exc_L4").get_segments()[0].get_stored_esyn_ids())
    t0 = time.time()
    simulation_run_time = 0
    for i, experiment in enumerate(experiment_list):
        logger.info("Starting experiment: " + experiment.__class__.__name__)
        stimuli = experiment.return_stimuli()
        unpresented_stimuli_indexes = data_store.identify_unpresented_stimuli(stimuli)
        logger.info("Running model")
        simulation_run_time += experiment.run(data_store, unpresented_stimuli_indexes)
        logger.info("Experiment %d/%d finished" % (i + 1, len(experiment_list)))
    # print("controller data_store get_stored_esyn_ids 1",
    #      param_filter_query(data_store, sheet_name="V1_Exc_L4").get_segments()[0].get_stored_esyn_ids())
    
    total_run_time = time.time() - t0
    mozaik_run_time = total_run_time - simulation_run_time
    
    logger.info("Total simulation run time: %.0fs" % total_run_time)
    logger.info(
        "Simulator run time: %.0fs (%d%%)"
        % (simulation_run_time, int(simulation_run_time / total_run_time * 100))
    )
    logger.info(
        "Mozaik run time: %.0fs (%d%%)"
        % (mozaik_run_time, int(mozaik_run_time / total_run_time * 100))
    )
    # print("controller data_store get_stored_esyn_ids 2",
    #      param_filter_query(data_store, sheet_name="V1_Exc_L4").get_segments()[0].get_stored_esyn_ids())

    return data_store
