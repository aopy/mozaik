# encoding: utf-8
"""
Mozaik connector interface.
"""
from collections import Counter
import logging
logging.basicConfig(filename='mozaik.log', level=logging.DEBUG)
import math
import time

from parameters import ParameterSet, ParameterDist
from pyNN import random, space
import numpy as np
import scipy

from ..core import BaseComponent
from ..sheets.vision import SheetWithMagnificationFactor
from ..tools.misc import sample_from_bin_distribution, normal_function

logger = logging.getLogger(__name__)


class Connector(BaseComponent):
    """
    An abstract interface class for Connectors in mozaik. Each mozaik connector should derive from this class and implement
    the _connect function. The usage is: create the instance of MozaikConnector and call connect() to realize the connections.
    """

    required_parameters = ParameterSet(
        {
            "target_synapses": str,
            "short_term_plasticity": ParameterSet(
                {"U": float, "tau_rec": float, "tau_fac": float, "tau_psc": float}
            )
        }
    )

    def __init__(self, model, name, source, target, parameters):
        logger.info(
            "Creating %s between %s and %s"
            % (
                self.__class__.__name__,
                source.__class__.__name__,
                target.__class__.__name__
            )
        )
        BaseComponent.__init__(self, model, parameters)
        self.name = name
        self.model.register_connector(self)
        self.sim = self.model.sim
        self.source = source
        self.target = target
        self.input = source
        self.target.input = self

        # This scaler has to be always applied to all weights just before sent to pyNN connect command
        self.weight_scaler = 1.0
        # This is because certain pyNN synaptic models interpret weights with different units and the Connector
        # function here corrects for these - ie. the Connectors in Mozaik will always assume the weights to be in nano-siemens
        if self.parameters.short_term_plasticity != None:
            self.weight_scaler = 1000.0

    def init_synaptic_mechanisms(self, weight=None, delay=None):
        # print("self.sim ", dir(self.sim))
        # print("weight ", weight)
        # print("delay ", delay)
        # print("self.parameters.short_term_plasticity ", self.parameters.short_term_plasticity)
        # print("not self.parameters.short_term_plasticity != None ", not self.parameters.short_term_plasticity != None )
        # tsodyks_synapse/short term plasticity is not supported by SpiNNaker
        # if not self.parameters.short_term_plasticity != None:
        if delay is not None and delay > 14.4:
            print("init_synaptic_mechanisms delay larger than 14.4 ", delay)
            print(type(delay))
            delay = 14.4
        # print("StaticSynapse")
        sm = self.sim.StaticSynapse(weight=weight, delay=delay)
        # if self.parameters.short_term_plasticity != None:
        #    print("StaticSynapse")
        #    sm = self.sim.StaticSynapse(weight=weight, delay=delay)
        # else:
        #    if weight != None:
        #        sm = self.sim.native_synapse_type("tsodyks_synapse")(
        #            weight=weight, delay=delay, **self.parameters.short_term_plasticity
        #        )
        #    else:
        #        sm = self.sim.native_synapse_type("tsodyks_synapse")(
        #            **self.parameters.short_term_plasticity
        #        )
        return sm

    def connect(self):
        t0 = time.time()
        self._connect()
        connect_time = time.time() - t0
        # logger.info(
        #    "Connector %s took %.0fs to compute"
        #    % (self.__class__.__name__, connect_time)
        # )

    def _connect(self):
        raise NotImplementedError

    def connection_field_plot_continuous(self, index, afferent=True, density=30):
        weights = np.array(self.proj.get("weight", format="list", gather=True))
        if afferent:
            idx = np.array(np.flatnonzero(weights[:, 1].flatten() == index))
            x = self.proj.pre.positions[0][weights[idx, 0].astype(int)]
            y = self.proj.pre.positions[1][weights[idx, 0].astype(int)]
            w = weights[idx, 2]
        else:
            idx = np.flatnonzero(weights[:, 0] == index)
            x = self.proj.post.positions[0][weights[idx, 1].astype(int)]
            y = self.proj.post.positions[1][weights[idx, 1].astype(int)]
            w = weights[idx, 2]

        xi = np.linspace(min(x), max(x), 100)
        yi = np.linspace(min(y), max(y), 100)
        zi = scipy.interpolate.griddata(x, y, w, xi, yi)
        # plt.figure()
        # plt.imshow(zi)
        # plt.scatter(x,y,marker='o',c=w,s=50)
        # plt.xlim(-self.source.parameters.sx/2,self.source.parameters.sx/2)
        # plt.ylim(-self.source.parameters.sy/2,self.source.parameters.sy/2)
        # plt.colorbar()
        # plt.title('Connection field from %s to %s of neuron %d' % (self.source.name,
        #                                                             self.target.name,
        #                                                             index))
        # plt.colorbar()

    def store_connections(self, datastore):
        from ..analysis.data_structures import Connections

        weights = self.proj.get("weight", format="list", gather=True)
        delays = self.proj.get("delay", format="list", gather=True)
        # print(self.name)
        datastore.add_analysis_result(
            Connections(
                weights,
                delays,
                source_size=(self.source.size_x, self.source.size_y),
                target_size=(self.target.size_x, self.target.size_y),
                proj_name=self.name,
                source_name=self.source.name,
                target_name=self.target.name,
                analysis_algorithm="connection storage"
            )
        )


class SpecificArborization(Connector):
    """
    Generic connector which gets directly list of connections as the list of
    quadruplets as accepted by the pyNN FromListConnector.
    This connector cannot be parametrized directly via the parameter file
    because that does not support list of tuples.
    This connector also gets rid of very weak synapses (below one-hundreth of the maximum synapse)
    """

    required_parameters = ParameterSet(
        {
            # the overall (sum) weight that a single target neuron should receive
            "weight_factor": float
        }
    )

    def __init__(
        self, network, source, target, connection_matrix, delay_matrix, parameters, name
    ):
        Connector.__init__(self, network, name, source, target, parameters)
        self.connection_matrix = connection_matrix
        self.delay_matrix = delay_matrix

    def _connect(self):
        X = np.zeros(self.connection_matrix.shape)
        Y = np.zeros(self.connection_matrix.shape)

        for x in range(0, X.shape[0]):
            for y in range(0, X.shape[1]):
                X[x][y] = x
                Y[x][y] = y

        for i in range(0, self.target.pop.size):
            self.connection_matrix[:, i] = (
                self.connection_matrix[:, i]
                / np.sum(self.connection_matrix[:, i])
                * self.parameters.weight_factor
            )

        # This is due to native synapses models (which we currently use as the short term synaptic plasticity model)
        # do not apply the 1000 factor scaler as the pyNN synaptic models
        self.connection_matrix = self.connection_matrix * self.weight_scaler
        self.connection_list = list(
            zip(
                np.array(X).flatten(),
                np.array(Y).flatten(),
                self.connection_matrix.flatten(),
                self.delay_matrix.flatten()
            )
        )
        # get rid of very weak synapses
        z = np.max(self.connection_matrix.flatten())
        self.connection_list = [
            (int(a), int(b), c, d)
            for (a, b, c, d) in self.connection_list
            if c > (z / 100.0)
        ]
        method = self.sim.FromListConnector(self.connection_list)
        self.proj = self.sim.Projection(
            self.source.pop,
            self.target.pop,
            method,
            synapse_type=self.init_synaptic_mechanisms(),
            label=self.name,
            rng=None,
            receptor_type=self.parameters.target_synapses
        )


class SpecificProbabilisticArborization(Connector):
    """
    Generic connector which gets directly list of connections as the list
    of quadruplets as accepted by the pyNN FromListConnector.
    It interprets the weights as proportional probabilities of connectivity,
    and for each neuron out connections it samples num_samples of
    connections that actually get realized according to these weights.
    Each such sample connections will have weight equal to
    weight_factor/num_samples but note that there can be multiple
    connections between a pair of neurons in this sample (in which case the
    weights are set to the multiple of the base weights times the number of
    occurrences in the sample).
    This connector cannot be parameterized directly via the parameter file
    because that does not support list of tuples.
    """

    required_parameters = ParameterSet(
        {
            # the overall strength of synapses in this connection per neuron (in µS) (i.e. the sum of the strength of synapses in this connection per target neuron)
            "weight_factor": float,
            "num_samples": int
        }
    )

    def __init__(
        self, network, source, target, connection_matrix, delay_matrix, parameters, name
    ):
        Connector.__init__(self, network, name, source, target, parameters)
        self.connection_matrix = connection_matrix
        self.delay_matrix = delay_matrix

    def _connect(self):
        # This is due to native synapses models (which we currently use as the short term synaptic plasticity model)
        # do not apply the 1000 factor scaler as the pyNN synaptic models
        wf = self.parameters.weight_factor * self.weight_scaler
        weights = self.connection_matrix
        delays = self.delay_matrix
        cl = []
        for i in range(0, self.target.pop.size):
            co = Counter(
                sample_from_bin_distribution(
                    weights[:, i].flatten(), int(self.parameters.num_samples)
                )
            )
            cl.extend(
                [
                    (
                        int(k),
                        int(i),
                        wf * co[k] / self.parameters.num_samples,
                        delays[k][i]
                    )
                    for k in list(co.keys())
                ]
            )

        method = self.sim.FromListConnector(cl)

        self.proj = self.sim.Projection(
            self.source.pop,
            self.target.pop,
            method,
            synapse_type=self.init_synaptic_mechanisms(),
            label=self.name,
            receptor_type=self.parameters.target_synapses
        )
