# encoding: utf-8
from collections import Counter
import ast
import logging
logging.basicConfig(filename='mozaik.log', level=logging.DEBUG)
import numpy
from collections import OrderedDict

from parameters import ParameterSet, ParameterDist

from . import Connector
from .. import load_component
from ..tools.misc import sample_from_bin_distribution
from .modular_connector_functions import ModularConnectorFunction
from mozaik.tools.distribution_parametrization import PyNNDistribution

logger = logging.getLogger(__name__)


class ExpVisitor(ast.NodeVisitor):
    """
    AST tree visitor used for determining list of variables in the delay or weight expresions
    """

    def __init__(self, **params):
        ast.NodeVisitor.__init__(self, **params)
        self.names = []

    def visit_Name(self, node):
        node.id
        if not (node.id in self.names):
            self.names.append(node.id)


class ModularConnector(Connector):
    """
    An abstract connector than allows for mixing of various factors that can affect the connectivity.
    The connector sepparates the implementation of delays from the implementation of weights.
    It receives a dictionary of weight functions and a dictonary of delays functions each being an instance of ModularConnectorFunction.
    In both cases the list of functions is combined by using expression string which is a parameter of this class (see parameters for details).
    The values returned by the ModularConnectorFunction will be considered to be in miliseconds when used for specifying delays, or the units used by pyNN for weights
    in case of specifying weights.
    The ModularConnector then sets such computed values of weights and delays directly in the connections.
    """

    required_parameters = ParameterSet(
        {
            # a dictionary of ModularConnectorFunction's and their parameters that will be used to determine the weights.
            "weight_functions": ParameterSet,
            # strucutured as follows
            #            {
            #                 component : 'class_name_of_the_ModularConnectorFunction',
            #                 params : {
            #                           ...
            #                         }
            #             }
            "delay_functions": ParameterSet,  # the same as weight_functions but for delays
            "weight_expression": str,  # a python expression that can use variables f1..fn where n is the number of functions in weight_functions, and fi corresponds to the name given to a ModularConnectorFunction in weight_function ParameterSet. It determines how are the weight functions combined to obtain the weights
            "delay_expression": str,  # a python expression that can use variables f1..fn where n is the number of functions in delays_functions, and fi corresponds to the name given to a ModularConnectorFunction in delays_function ParameterSet. It determines how are the delays functions combined to obtain the delays
        }
    )

    def __init__(self, network, name, source, target, parameters):
        Connector.__init__(self, network, name, source, target, parameters)

        # lets load up the weight ModularConnectorFunction's
        self.weight_functions = {}
        self.delay_functions = {}
        self.simulator_time_step = self.sim.get_time_step()
        # lets determine the list of variables in weight expressions
        v = ExpVisitor()
        v.visit(ast.parse(self.parameters.weight_expression))
        self.weight_function_names = v.names
        # lets determine the list of variables in delay expressions
        v = ExpVisitor()
        v.visit(ast.parse(self.parameters.delay_expression))
        self.delay_function_names = v.names
        # print("self.source ", self.source)
        # print("self.target ", self.target)
        # print("self.parameters.weight_functions ", self.parameters.weight_functions)
        for k in self.weight_function_names:
            self.weight_functions[k] = load_component(
                self.parameters.weight_functions[k].component
            )(self.source, self.target, self.parameters.weight_functions[k].params)
            assert isinstance(self.weight_functions[k], ModularConnectorFunction)

        for k in self.delay_function_names:
            self.delay_functions[k] = load_component(
                self.parameters.delay_functions[k].component
            )(self.source, self.target, self.parameters.delay_functions[k].params)

    def _obtain_weights(self, i):
        """
        This function calculates the combined weights from the ModularConnectorFunction in weight_functions
        """
        # print("weights for", i)
        # evaled = {}
        evaled = OrderedDict()
        # print('i ', i)
        # print("self.weight_function_names ", self.weight_function_names)
        # print("self.weight_functions ", self.weight_functions)
        # print("self.weight_functions[f1].evaluate(1)", self.weight_functions["f1"].evaluate(1))
        for k in self.weight_function_names:
            evaled[k] = self.weight_functions[k].evaluate(i)
        # print("evaled ", evaled["f1"].shape)
        # print("self.parameters.weight_expression ", self.parameters.weight_expression)
        # print("self.source.pop.size ", self.source.pop.size)
        # print("globals ", globals())
        # print("numpy.zeros((self.source.pop.size,)) ", numpy.zeros((self.source.pop.size,)))
        # print("eval(self.parameters.weight_expression, globals(), evaled) ",
        #       eval(self.parameters.weight_expression, globals(), evaled))
        # print("end")
        return numpy.zeros((self.source.pop.size,)) + eval(
            self.parameters.weight_expression, globals(), evaled
        )

    def _obtain_delays(self, i):
        """
        This function calculates the combined weights from the ModularConnectorFunction in weight_functions
        """
        evaled = {}
        for k in self.delay_function_names:
            evaled[k] = self.delay_functions[k].evaluate(i)

        delays = numpy.zeros((self.source.pop.size,)) + eval(
            self.parameters.delay_expression, globals(), evaled
        )
        # round to simulation step
        delays = (
            numpy.rint(delays / self.simulator_time_step) * self.simulator_time_step
        )
        # print("delays in connectors modular ", delays)
        # for time steps of 0.1ms, the maximum supported delay is 14.4ms for SpiNNaker
        for i, d in enumerate(delays):
            # if d > 12.7:
            if d > 14.4:
                # if d > 144:
                # print("delay larger than 14.4 ", d)
                # print("i ", i)
                # print("type d", type(d))
                delays[i] = 14.4
                # delays[i] = 144
                # delays[i] = 12.7

        return delays

    def _connect(self):
        connection_list = []
        z = numpy.zeros((self.target.pop.size,))
        # for i in numpy.nonzero(self.target.pop._mask_local)[0]:
        if hasattr(self.target.pop, "_mask_local"):
            indices = numpy.nonzero(self.target.pop._mask_local)[0]
        else:
            indices = numpy.arange(self.target.pop.size)
        for i in indices:
            connection_list.extend(
                list(
                    zip(
                        numpy.arange(0, self.source.pop.size, 1),
                        z + i,
                        self.weight_scaler * self._obtain_weights(i).flatten(),
                        self._obtain_delays(i).flatten()
                    )
                )
            )

        self.method = self.sim.FromListConnector(connection_list)
        print("projection method ", self.method)
        print("self.source.pop ", self.source.pop)
        print("self.target.pop ", self.target.pop)
        self.proj = self.sim.Projection(
            self.source.pop,
            self.target.pop,
            self.method,
            synapse_type=self.init_synaptic_mechanisms(),
            label=self.name,
            receptor_type=self.parameters.target_synapses
        )


class ModularSamplingProbabilisticConnector(ModularConnector):
    """
    ModularConnector that interprets the weights as proportional probabilities of connectivity
    and for each neuron in connections it samples num_samples of
    connections that actually get realized according to these weights.
    Each such sample connections will have weight equal to
    base_weight but note that there can be multiple
    connections between a pair of neurons in this sample (in which case the
    weights are set to the multiple of the base weights times the number of
    occurrences in the sample).
    """

    required_parameters = ParameterSet(
        {"num_samples": PyNNDistribution, "base_weight": PyNNDistribution}
    )

    def _connect(self):
        cl = []
        v = 0
        # for i in numpy.nonzero(self.target.pop._mask_local)[0]:
        if hasattr(self.target.pop, "_mask_local"):
            indices = numpy.nonzero(self.target.pop._mask_local)[0]
        else:
            indices = numpy.arange(self.target.pop.size)
        for i in indices:
            weights = self._obtain_weights(i)
            delays = self._obtain_delays(i)
            co = Counter(
                sample_from_bin_distribution(
                    weights, int(self.parameters.num_samples.next())
                )
            )
            v = v + numpy.sum(list(co.values()))
            k = list(co.keys())
            a = numpy.array(
                [
                    k,
                    numpy.zeros(len(k)) + i,
                    self.weight_scaler
                    * numpy.multiply(
                        self.parameters.base_weight.next(len(k)), list(co.values())
                    ),
                    numpy.array(delays)[k]
                ]
            )
            cl.append(a)
        # print("weights obtained")
        cl = numpy.hstack(cl).T
        method = self.sim.FromListConnector(cl)
        print("ModularSamplingProbabilisticConnector")
        print("projection method ", method)
        print("self.source.pop ", self.source.pop)
        print("self.target.pop ", self.target.pop)

        logger.warning(
            "%s(%s): %g connections were created, %g per target neuron [%g]"
            % (
                self.name,
                self.__class__.__name__,
                len(cl),
                # len(cl) / len(numpy.nonzero(self.target.pop._mask_local)[0]),
                # v / len(numpy.nonzero(self.target.pop._mask_local)[0])
                len(cl) / len(numpy.arange(self.target.pop.size)),
                v / len(numpy.arange(self.target.pop.size))
            )
        )

        if len(cl) > 0:
            self.proj = self.sim.Projection(
                self.source.pop,
                self.target.pop,
                method,
                synapse_type=self.init_synaptic_mechanisms(),
                label=self.name,
                receptor_type=self.parameters.target_synapses
            )
        else:
            logger.warning(
                "%s(%s): empty projection - pyNN projection not created."
                % (self.name, self.__class__.__name__)
            )


class ModularSingleWeightProbabilisticConnector(ModularConnector):
    """
    ModularConnector that interprets the weights as proportional probabilities of connectivity.
    The parameter connection_probability is interepreted as the average probability that two neurons will be connected in this
    projection. For each pair this connecter will make one random choice of connecting them (where the probability of this choice
    is determined as the proportional probability of the corresponding weight normalized by the connection_probability parameter).
    It will set each connections to the weight base_weight.
    """

    required_parameters = ParameterSet(
        {"connection_probability": float, "base_weight": PyNNDistribution}
    )

    def _connect(self):
        cl = []
        # for i in numpy.nonzero(self.target.pop._mask_local)[0]:
        if hasattr(self.target.pop, "_mask_local"):
            indices = numpy.nonzero(self.target.pop._mask_local)[0]
        else:
            indices = numpy.arange(self.target.pop.size)
        for i in indices:
            weights = self._obtain_weights(i)
            delays = self._obtain_delays(i)
            conections_probabilities = (
                weights
                / numpy.sum(weights)
                * self.parameters.connection_probability
                * len(weights)
            )
            connection_indices = numpy.flatnonzero(
                conections_probabilities
                > numpy.random.rand(len(conections_probabilities))
            )
            cl.extend(
                [
                    (
                        k,
                        i,
                        self.weight_scaler * self.parameters.base_weight.next(),
                        delays[k]
                    )
                    for k in connection_indices
                ]
            )

        method = self.sim.FromListConnector(cl)
        print("ModularSingleWeightProbabilisticConnector")
        print("projection method ", method)
        print("self.source.pop ", self.source.pop)
        print("self.target.pop ", self.target.pop)
        logger.warning(
            "%s: %g %g",
            self.name,
            min(conections_probabilities),
            max(conections_probabilities)
        )
        logger.warning(
            "%s: %d connections  [,%g,%g,%g]",
            self.name,
            len(cl),
            self.parameters.connection_probability,
            numpy.sum(weights),
            len(weights)
        )

        if len(cl) > 0:
            self.proj = self.sim.Projection(
                self.source.pop,
                self.target.pop,
                method,
                synapse_type=self.init_synaptic_mechanisms(),
                label=self.name,
                receptor_type=self.parameters.target_synapses
            )
        else:
            logger.warning(
                "%s(%s): empty projection - pyNN projection not created."
                % (self.name, self.__class__.__name__)
            )


class ModularSamplingProbabilisticConnectorAnnotationSamplesCount(ModularConnector):
    """
    ModularConnector that interprets the weights as proportional probabilities of connectivity
    and for each neuron in connections it samples num_samples of
    connections that actually get realized according to these weights.
    Each such sample connections will have weight equal to
    base_weight but note that there can be multiple
    connections between a pair of neurons in this sample (in which case the
    weights are set to the multiple of the base weights times the number of
    occurrences in the sample).
    """

    required_parameters = ParameterSet(
        {
            "annotation_reference_name": str,
            "num_samples": int,
            "base_weight": PyNNDistribution
        }
    )

    def worker(self, ref, idxs):
        cl = []
        for i in idxs:
            samples = self.target.get_neuron_annotation(
                i, self.parameters.annotation_reference_name
            )
            weights = self._obtain_weights(i)
            delays = self._obtain_delays(i)
            if self.parameters.num_samples == 0:
                co = Counter(sample_from_bin_distribution(weights, int(samples)))
            else:
                # AssertionError: V1L4ExcL4ExcConnection: 64 110
                assert self.parameters.num_samples > 2 * int(samples), "%s: %d %d" % (
                    self.name,
                    self.parameters.num_samples,
                    2 * int(samples)
                )
                a = sample_from_bin_distribution(
                    weights, int(self.parameters.num_samples - 2 * int(samples))
                )
                co = Counter(a)
            v = v + numpy.sum(list(co.values()))
            cl.extend(
                [
                    (
                        int(k),
                        int(i),
                        self.weight_scaler
                        * self.parameters.base_weight.next()
                        * co[k],
                        delays[k]
                    )
                    for k in list(co.keys())
                ]
            )
        return cl

    def _connect(self):
        cl = []
        v = 0
        if hasattr(self.target.pop, "_mask_local"):
            indices = numpy.nonzero(self.target.pop._mask_local)[0]
        else:
            indices = numpy.arange(self.target.pop.size)
        # print("self.target.pop.size ", self.target.pop.size)
        # print("indices ", indices)
        for i in indices:
            # for i in numpy.nonzero(self.target.pop._mask_local)[0]:
            samples = self.target.get_neuron_annotation(
                i, self.parameters.annotation_reference_name
            )
            weights = self._obtain_weights(i)
            delays = self._obtain_delays(i)
            if self.parameters.num_samples == 0:
                co = Counter(sample_from_bin_distribution(weights, int(samples)))
            else:
                assert self.parameters.num_samples > 2 * int(samples), "%s: %d %d" % (
                    self.name,
                    self.parameters.num_samples,
                    2 * int(samples)
                )
                co = Counter(
                    sample_from_bin_distribution(
                        weights, int(self.parameters.num_samples - 2 * int(samples))
                    )
                )
            v = v + numpy.sum(list(co.values()))
            k = list(co.keys())
            a = numpy.array(
                [
                    k,
                    numpy.zeros(len(k)) + i,
                    self.weight_scaler
                    * numpy.multiply(
                        self.parameters.base_weight.next(len(k)), list(co.values())
                    ),
                    numpy.array(delays)[k]
                ]
            )
            cl.append(a)

        cl = numpy.hstack(cl).T
        method = self.sim.FromListConnector(cl)
        print("ModularSamplingProbabilisticConnectorAnnotationSamplesCount")
        print("projection method ", method)
        print("self.source.pop ", self.source.pop)
        print("self.target.pop ", self.target.pop)

        logger.warning(
            "%s(%s): %g connections were created, %g per target neuron [%g]"
            % (
                self.name,
                self.__class__.__name__,
                len(cl),
                # len(cl) / len(numpy.nonzero(self.target.pop._mask_local)[0]),
                # v / len(numpy.nonzero(self.target.pop._mask_local)[0])
                len(cl) / len(numpy.arange(self.target.pop.size)),
                v / len(numpy.arange(self.target.pop.size))
            )
        )

        if len(cl) > 0:
            self.proj = self.sim.Projection(
                self.source.pop,
                self.target.pop,
                method,
                synapse_type=self.init_synaptic_mechanisms(),
                label=self.name,
                receptor_type=self.parameters.target_synapses
            )
        else:
            logger.warning(
                "%s(%s): empty projection - pyNN projection not created."
                % (self.name, self.__class__.__name__)
            )
