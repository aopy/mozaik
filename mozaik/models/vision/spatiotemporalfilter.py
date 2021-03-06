# encoding: utf-8
"""
Retina/LGN model based on that developed by Jens Kremkow (CNRS-INCM/ALUF)
"""
import logging
logging.basicConfig(filename='mozaik.log', level=logging.DEBUG)
import os.path
import pickle
# import _pickle as cPickle

from parameters import ParameterSet
import numpy

import mozaik
from . import cai97
from ... import get_seeds
from ...core import SensoryInputComponent
from ...sheets.vision import RetinalUniformSheet
from ...space import VisualSpace, VisualRegion
from ...tools.misc import load_pickle_crosscompat
from ...tools.mozaik_parametrized import MozaikParametrized
from collections import OrderedDict

logger = logging.getLogger(__name__)


def meshgrid3D(x, y, z):
    """A slimmed-down version of http://www.scipy.org/scipy/numpy/attachment/ticket/966/meshgrid.py"""
    x = numpy.asarray(x)
    y = numpy.asarray(y)
    z = numpy.asarray(z)
    mult_fact = numpy.ones((len(x), len(y), len(z)))
    nax = numpy.newaxis
    return (
        x[:, nax, nax] * mult_fact,
        y[nax, :, nax] * mult_fact,
        z[nax, nax, :] * mult_fact
    )


class SpatioTemporalReceptiveField(object):
    """
    Implements spatio-temporal receptive field.
    Parameters
    ----------
    func : function
        should be a function of x, y, t, and a ParameterSet object
    func_params : ParameterSet
        ParameterSet that is passed to `func`.
    width : float (degrees)
        x-dimension size
    height : float (degrees)
        y-dimension size
    duration : float (ms)
        length of the temporal axis of the RF
    Notes
    -----
    Coordinates x = 0 and y = 0 are at the centre of the spatial kernel.
    """

    def __init__(self, func, func_params, width, height, duration):
        self.func = func
        self.func_params = func_params
        self.width = float(width)
        self.height = float(height)
        self.duration = float(duration)
        self.kernel = None
        self.spatial_resolution = numpy.inf
        self.temporal_resolution = numpy.inf

    def quantize(self, dx, dy, dt):
        """
        Quantizes the the receptive field.
        Parameters
        ----------
        dx : float
           The number of pixels along x axis.
        dy : float
           The number of pixels along y axis.
        dy : float
           The number of time bins along the t axis.
        Notes
        -----
        If `dx` does not
        divide exactly into the width, then the actual width will be slightly
        larger than the nominal width. `dx` and `dy` should be in degrees and `dt` in ms.
        """
        assert dx == dy  # For now, at least
        nx = numpy.ceil(self.width / dx)
        ny = numpy.ceil(self.height / dy)
        nt = numpy.ceil(self.duration / dt)
        width = nx * dx
        height = ny * dy
        duration = nt * dt
        # x and y are the coordinates of the centre of each pixel
        # I use linspace instead of arange as arange sometimes can return inconsistent number of elements
        # (must have something to do with rounding errors)
        # x = numpy.arange(0.0, width, dx)  + dx/2.0 - width/2.0
        # y = numpy.arange(0.0, height, dy) + dy/2.0 - height/2.0

        x = numpy.linspace(0.0, width - dx, int(width / dx)) + dx / 2.0 - width / 2.0
        y = numpy.linspace(0.0, height - dy, int(height / dy)) + dx / 2.0 - height / 2.0

        # t is the time at the beginning of each timestep
        t = numpy.arange(0.0, duration, dt)
        X, Y, T = meshgrid3D(y, x, t)  # x,y are reversed because (x,y) <--> (j,i)
        kernel = self.func(X, Y, T, self.func_params)
        # logger.debug("Created receptive field kernel: width=%gº, height=%gº, duration=%g ms, shape=%s" %
        #                 (width, height, duration, kernel.shape))
        # logger.debug("before normalization: min=%g, max=%g" %
        #                 (kernel.min(), kernel.max()))
        kernel = kernel / (
            nx * ny * nt
        )  # normalize to make the kernel sum quasi-independent of the quantization
        # logger.debug("  after normalization: min=%g, max=%g, sum=%g" %
        #                 (kernel.min(), kernel.max(), kernel.sum()))
        self.kernel = kernel
        self.spatial_resolution = dx
        self.temporal_resolution = dt
        self.reshaped_kernel = self.kernel.reshape(-1, numpy.shape(self.kernel)[2]).T

    @property
    def kernel_duration(self):
        return self.kernel.shape[2]

    def __str__(self):
        s = "Receptive field: width=%gº, height=%gº, duration=%g ms" % (
            self.width,
            self.height,
            self.duration
        )
        if self.kernel is not None:
            k = self.kernel
            h, w = k.shape[0:2]
            s += (
                ", quantization=%s, actual width=%gº, actual_height=%gº, min=%g,"
                " max=%g."
                % (
                    k.shape,
                    w * self.spatial_resolution,
                    h * self.spatial_resolution,
                    k.min(),
                    k.max()
                )
            )
        else:
            s += ". Not quantized."
        return s


class CellWithReceptiveField(object):
    """
    A model of the input current to an LGN relay cell, that multiplies, in space
    and time, the luminance values impinging on its receptive field by a
    spatiotemporal kernel. Spatial summation over the result of this
    multiplication at each time point gives a current in nA, that may then be
    injected into a relay cell.
    initialize() should be called once, before stimulus presentation
    view() should be called in a loop, once for each stimulus frame
    response_current() should be called at the end of stimulus presentation
    Parameters
    ----------
    x , y : float
          x and y coordinates of the center of the RF in visual space.
    receptive_field : SpatioTemporalReceptiveField
          The receptive field object containing the RFs data.
    gain_control : float
         The calculated input current values will be multiplied by the gain parameter.
    visual_space : VisualSpace
                 The object representing the visual space.
    """

    def __init__(self, x, y, receptive_field, gain_control, visual_space):
        self.x = x  # position in space
        self.y = y  #
        self.visual_space = visual_space
        assert isinstance(receptive_field, SpatioTemporalReceptiveField)
        self.receptive_field = receptive_field
        self.gain_control = (
            gain_control  # (nA.m²/cd) could imagine making this a function of
        )
        # the background luminance
        self.i = 0
        self.visual_region = VisualRegion(
            location_x=self.x,
            location_y=self.y,
            size_x=self.receptive_field.width,
            size_y=self.receptive_field.height
        )
        # logger.debug("view_array.shape = %s" % str(view_array.shape))
        # logger.debug("receptive_field.kernel.shape = %s" % str(self.receptive_field.kernel.shape))
        # logger.debug("response.shape = %s" % str(self.response.shape))
        if visual_space.update_interval % self.receptive_field.temporal_resolution != 0:
            errmsg = (
                "The receptive field temporal resolution (%g ms) must be an integer"
                " multiple of the visual space update interval (%g ms)"
                % (
                    self.receptive_field.temporal_resolution,
                    visual_space.update_interval
                )
            )
            raise Exception(errmsg)
        self.update_factor = int(
            visual_space.update_interval / self.receptive_field.temporal_resolution
        )

        # logger.debug("Created cell with receptive field centred at %gº,%gº" % (x,y))
        # logger.debug("  " + str(receptive_field))

    def initialize(self, background_luminance, stimulus_duration):
        """
        Create the array that will contain the current response, and set the
        initial values on the assumption that the system was looking at a blank
        screen of constant luminance prior to stimulus onset.
        Parameters
        ----------
        background_luminance : float
                             The background luminance of the visual space.
        stimulus_duration : float (ms)
                          The duration  of the visual stimulus.
        """
        # we add some extra padding to avoid having to check for index out-of-bounds in view()
        self.response_length = int(
            numpy.ceil(stimulus_duration / self.receptive_field.temporal_resolution)
            + self.receptive_field.kernel_duration
        )
        # we should initialize based on multiplying the kernel by the background activity
        # R0 = K_0.I_0 + Sum[j=1,L-1] K_j.B
        # R1 = K_0.I_1 + K_1.I_0 + Sum[j=2,L-1] K_j.B
        # the image-dependent components will be added in view(), so we need to

        # initialize with the Sum[] k_j.B components
        self.background_luminance = background_luminance
        self.response = numpy.zeros((self.response_length,))
        self.std = numpy.zeros((self.response_length,))
        self.mean = numpy.zeros((self.response_length,))
        L = self.receptive_field.kernel_duration
        assert L <= self.response_length

        for i in range(L):
            self.response[i] += (
                background_luminance
                * self.receptive_field.kernel[:, :, i + 1 : L].sum()
            )

        for i in range(L):
            self.response[-(i + 1)] += (
                background_luminance
                * self.receptive_field.kernel[:, :, 0 : L - i].sum()
            )
        self.i = 0

    def view(self):
        """
        Look at the visual space and update t
        Where the kernel temporal resolution is the same as the frame duration
        (visual space update interval):
           R_i = Sum[j=0,L-1] K_j.I_i-j
             where L is the kernel length/duration
        Where the kernel temporal resolution = (frame duration)/α (α an integer)
           R_k = Sum[j=0,L-1] K_j.I_i'
             where i' = (k-j)//α  (// indicates integer division, discarding the
             remainder)
        To avoid loading the entire image sequence into memory, we build up the response array one frame at a time.
        """
        view_array = self.visual_space.view(
            self.visual_region, pixel_size=self.receptive_field.spatial_resolution
        )
        self.std[self.i : self.i + self.update_factor] = numpy.std(view_array)
        self.mean[self.i : self.i + self.update_factor] = numpy.mean(view_array)
        time_course = numpy.dot(
            self.receptive_field.reshaped_kernel,
            view_array.reshape(-1)[: numpy.newaxis]
        )

        self.va = view_array

        if self.update_factor != 1.0:
            for j in range(self.i, self.i + self.update_factor):
                self.response[
                    j : j + self.receptive_field.kernel_duration
                ] += time_course[
                    : len(self.response[j : j + self.receptive_field.kernel_duration])
                ]  # / self.update_factor
        else:
            self.response[
                self.i : self.i + self.receptive_field.kernel_duration
            ] += time_course[
                : len(
                    self.response[
                        self.i : self.i + self.receptive_field.kernel_duration
                    ]
                )
            ]

        self.i += (
            self.update_factor
        )  # we assume there is only ever 1 visual space used between initializations

    def response_current(self):
        """
        Multiply the response (units of luminance (cd/m²) if we assume the
        kernel values are dimensionless) by the 'gain', to produce a current in
        nA. Returns a dictionary containing 'times' and 'amplitudes'.
        """
        k = numpy.squeeze(
            numpy.mean(
                numpy.squeeze(
                    numpy.mean(numpy.abs(self.receptive_field.kernel), axis=0)
                ),
                axis=0
            )
        )
        # self.std = numpy.convolve(self.std,k[::-1]/numpy.sqrt(numpy.power(k,2).sum()),mode='same')
        self.std = numpy.convolve(self.std, k[::-1], mode="same")

        if self.gain_control.non_linear_gain != None:
            c = numpy.sum(self.receptive_field.kernel.flatten()) * self.mean
            L = self.receptive_field.kernel_duration
            for i in range(L):
                c[i] += (
                    self.background_luminance - numpy.mean(self.mean[:L])
                ) * self.receptive_field.kernel[:, :, i + 1 : L].sum()

            for i in range(L):
                c[-(i + 1)] += (
                    self.background_luminance - numpy.mean(self.mean[-L:])
                ) * self.receptive_field.kernel[:, :, 0 : L - i].sum()

            ta = (
                self.gain_control.gain
                * (self.response - c)
                / (self.gain_control.non_linear_gain.contrast_scaler * self.std + 1.0)
            )
            tb = (
                self.gain_control.non_linear_gain.luminance_gain
                * c
                / (self.gain_control.non_linear_gain.luminance_scaler * self.mean + 1.0)
            )
            response = (ta + tb)[
                : -self.receptive_field.kernel_duration
            ]  # remove the extra padding at the end
        # current response
        else:
            response = (
                self.gain_control.gain
                * self.response[: -self.receptive_field.kernel_duration]
            )  # remove the extra padding at the end
        time_points = self.receptive_field.temporal_resolution * numpy.arange(
            0, len(response)
        )

        # ylab.figure()
        # ylab.title(str(numpy.shape(self.receptive_field.kernel)))
        # ylab.subplot(3,1,1)
        # ylab.imshow(numpy.mean(self.receptive_field.kernel,axis=0))
        # ylab.title(str(numpy.shape(self.receptive_field.kernel)))
        # ylab.colorbar()
        # ylab.subplot(3,1,2)
        # ylab.imshow(self.va)
        # ylab.colorbar()
        # ylab.subplot(3,1,3)
        # ylab.plot(time_points,response)

        return {"times": time_points, "amplitudes": response}


class SpatioTemporalFilterRetinaLGN(SensoryInputComponent):
    """
    Retina/LGN model with spatiotemporal receptive field.
    Parameters
    ----------
    density : int (1/degree^2)
              Number of neurons to simulate per square degree of visual space.
    size : tuple (degree,degree)
         The x and y size of the visual field.
    linear_scaler : float
                  The linear scaler that the RF output is multiplied with.
    cached : bool
           If the stimuli are chached.
    cache_path : str
           Path to the directory where to store the create the cache.
    mpi_reproducible_noise : bool
           If true the background noise is generated in such a way that is reproducible accross runs using different number of mpi processes.
           Significant slowdown if True.
    Notes
    -----
    If the stimulus is cached SpatioTemporalFilterRetinaLGN will write in the local directory `parameters.cache_path`
    the generated amplitudes for all the neurons in the retina (so this will be specific to the model)
    for each new presented stimulus. If it is asked to generate activities for a stimulus that already exists in the directory (it just
    checks for the name and parameter values of the stimulus, *except* trail number) it will retrieve the values from the cahce.
    Note that the input currents are stored without the noise and the aditional noise is still applied after retrieval
    so the actual current injected into the retinal neurons will not be identical to the one that was injected when
    the stimulus was saved in the cache.
    **IMPORTANT**
    This mechanism assumes that the retinal model stays otherwise identical between
    simulations. The moment anything is changed in the retinal model one **has** to delete
    the retina_cache directory (which effectively resets the cache).
    """

    required_parameters = ParameterSet(
        {
            "density": int,  # neurons per degree squared
            "size": tuple,  # degrees of visual field
            "linear_scaler": float,  # linear scaler that the RF output is multiplied with
            "cached": bool,
            "cache_path": str,
            # if True, noise is precomputed and StepCurrentSource is used which makes it slower
            "mpi_reproducible_noise": bool,
            "recorders": ParameterSet,
            "recording_interval": float,
            "receptive_field": ParameterSet(
                {
                    "func": str,
                    "func_params": ParameterSet,
                    "width": float,
                    "height": float,
                    "spatial_resolution": float,
                    "temporal_resolution": float,
                    "duration": float
                }
            ),
            "cell": ParameterSet(
                {"model": str, "params": ParameterSet, "initial_values": ParameterSet}
            ),
            "gain_control": {
                "gain": float,
                "non_linear_gain": ParameterSet(
                    {
                        "luminance_gain": float,
                        "luminance_scaler": float,
                        "contrast_scaler": float
                    }
                )
            },
            # "noise": ParameterSet({"mean": float, "stdev": float}),  # nA
            "noise": ParameterSet({"mean": float, "stdev": float, "dt": float}),
        }
    )

    def __init__(self, model, parameters):
        SensoryInputComponent.__init__(self, model, parameters)
        self.shape = (self.parameters.density, self.parameters.density)
        self.sheets = OrderedDict()
        self._built = False
        self.rf_types = ('X_ON', 'X_OFF')
        sim = self.model.sim
        self.pops = OrderedDict()
        self.scs = OrderedDict()
        self.ncs = OrderedDict()
        self.ncs_rng = OrderedDict()
        self.internal_stimulus_cache = OrderedDict()
        for rf_type in self.rf_types:
            p = RetinalUniformSheet(model,
                                    ParameterSet({'sx': self.parameters.size[0],
                                                  'sy': self.parameters.size[1],
                                                  'density': self.parameters.density,
                                                  'cell': self.parameters.cell,
                                                  'name': rf_type,
                                                  'artificial_stimulators': OrderedDict(),
                                                  'recorders': self.parameters.recorders,
                                                  'recording_interval': self.parameters.recording_interval,
                                                  'mpi_safe': False}))
            self.sheets[rf_type] = p

        for rf_type in self.rf_types:
            self.scs[rf_type] = []
            # self.ncs[rf_type] = []
            self.ncs_rng[rf_type] = []
            seeds = mozaik.get_seeds((self.sheets[rf_type].pop.size,))
            for i, lgn_cell in enumerate(self.sheets[rf_type].pop.all_cells):
                if i == 0:
                   break
                scs = sim.StepCurrentSource(times=[0.0], amplitudes=[0.0])

                # if not self.parameters.mpi_reproducible_noise:
                    # print("noisy current created")
                #    ncs = sim.NoisyCurrentSource(**self.parameters.noise)
                # else:
                #    ncs = sim.StepCurrentSource(times=[0.0], amplitudes=[0.0])
                if True:
                    # if self.sheets[rf_type].pop._mask_local[i]:
                    # self.ncs_rng[rf_type].append(numpy.random.RandomState(seed=seeds[i]))
                    self.scs[rf_type].append(scs)
                    # self.ncs[rf_type].append(ncs)
                # lgn_cell.inject(scs)
                scs.inject_into(lgn_cell)
                # lgn_cell.inject(ncs)

            # inject noisy current to populations
            # print("injecting current")
            # scs = sim.StepCurrentSource(times=[0.0], amplitudes=[10.0])
            # self.sheets[rf_type].pop.inject(scs)
            # ncs = sim.NoisyCurrentSource(**self.parameters.noise)
            # self.sheets[rf_type].pop.inject(ncs)
            # ncs.inject_into(self.sheets[rf_type].pop)

        P_rf = self.parameters.receptive_field
        rf_function = eval(P_rf.func)

        rf_ON = SpatioTemporalReceptiveField(rf_function,
                                             P_rf.func_params,
                                             P_rf.width, P_rf.height,
                                             P_rf.duration)
        rf_OFF = SpatioTemporalReceptiveField(lambda x, y, t, p: -1.0 * rf_function(x, y, t, p),
                                              P_rf.func_params,
                                              P_rf.width, P_rf.height,
                                              P_rf.duration)

        dx = dy = P_rf.spatial_resolution
        dt = P_rf.temporal_resolution
        for rf in rf_ON, rf_OFF:
            rf.quantize(dx, dy, dt)

        self.rf = {'X_ON': rf_ON, 'X_OFF': rf_OFF}

    def __init__2(self, model, parameters):
        SensoryInputComponent.__init__(self, model, parameters)
        self.shape = (self.parameters.density, self.parameters.density)
        self.sheets = {}
        self._built = False
        self.rf_types = ("X_ON", "X_OFF")
        sim = self.model.sim
        self.pops = {}
        self.scs = {}
        self.ncs = {}
        self.ncs_rng = {}
        self.internal_stimulus_cache = {}
        for rf_type in self.rf_types:
            p = RetinalUniformSheet(
                model,
                ParameterSet(
                    {
                        "sx": self.parameters.size[0],
                        "sy": self.parameters.size[1],
                        "density": self.parameters.density,
                        "cell": self.parameters.cell,
                        "name": rf_type,
                        "artificial_stimulators": {},
                        "recorders": self.parameters.recorders,
                        "recording_interval": self.parameters.recording_interval,
                        "mpi_safe": False
                    }
                )
            )
            self.sheets[rf_type] = p
        # print("SIM ", dir(sim))
        # print(self.rf_types[8])  # induce error
        for rf_type in self.rf_types:
            # print("rf_type ", rf_type)
            self.scs[rf_type] = []
            self.ncs[rf_type] = []
            self.ncs_rng[rf_type] = []
            seeds = get_seeds((self.sheets[rf_type].pop.size,))
            # print("self.sheets[rf_type] ", self.sheets[rf_type])
            # print("self.sheets[rf_type].pop ", self.sheets[rf_type].pop)
            # print("type(self.sheets[rf_type]) ", type(self.sheets[rf_type]))
            # print("dir(self.sheets[rf_type]) ", dir(self.sheets[rf_type]))
            # print("seeds ", seeds)
            # print("self.sheets[rf_type].pop.all_cells ", self.sheets[rf_type].pop.all_cells)
            # if not self.parameters.mpi_reproducible_noise:
            #    ncs = sim.NoisyCurrentSource(**self.parameters.noise)
            #    ncs = sim.SpikeSourcePoisson  # test this
            #    sim.SpikeSourcePoisson
            #    np = sim.Population.SpikeSourcePoisson(rate=0)

            #    np = sim.Population(10, sim.SpikeSourcePoisson(rate=10.0), label="noise")
            #    sim.Projection(np, self.sheets[rf_type].pop, sim.AllToAllConnector())

            # print("SpikeSourcePoisson")
            # self.sheets[rf_type].pop(sim.SpikeSourcePoisson(rate=0))
            # for i, lgn_cell in enumerate(self.sheets[rf_type].pop.all_cells):
            #     print("lgn_cell ", lgn_cell)
            #     print("lgn_cell type ", type(lgn_cell))
            #     print("lgn_cell i offset ", lgn_cell.i_offset)
            #     print("not self.parameters.mpi_reproducible_noise", not self.parameters.mpi_reproducible_noise)
            #     lgn_cell.i_offset = lgn_cell.i_offset + 1  # test
            #     scs = sim.StepCurrentSource(times=[0.0], amplitudes=[0.0])  # update i_offset between calls to run()
            #     print("self.parameters.noise ",  self.parameters.noise)
            #     if not self.parameters.mpi_reproducible_noise:
            #        ncs = sim.NoisyCurrentSource(**self.parameters.noise)
            #        ncs = sim.SpikeSourcePoisson  # test this
            #        sim.SpikeSourcePoisson
            #     self.sheets[rf_type].pop(sim.SpikeSourcePoisson())
            #     else:
            #        pass
            #        ncs = sim.StepCurrentSource(times=[0.0], amplitudes=[0.0])

            #    if self.sheets[rf_type].pop._mask_local[i]:  # 'Population' object has no attribute '_mask_local'
            #        self.ncs_rng[rf_type].append(
            #            numpy.random.RandomState(seed=seeds[i])
            #        )
            #        self.scs[rf_type].append(scs)
            #        self.ncs[rf_type].append(ncs)
            #     lgn_cell.inject(scs)
            #     lgn_cell.inject(ncs)  # does inject work with SpikeSourcePoisson
            #     print("lgn_cell i offset 2", lgn_cell.i_offset)

        P_rf = self.parameters.receptive_field
        rf_function = eval(P_rf.func)

        rf_ON = SpatioTemporalReceptiveField(
            rf_function, P_rf.func_params, P_rf.width, P_rf.height, P_rf.duration
        )
        rf_OFF = SpatioTemporalReceptiveField(
            lambda x, y, t, p: -1.0 * rf_function(x, y, t, p),
            P_rf.func_params,
            P_rf.width,
            P_rf.height,
            P_rf.duration
        )
        dx = dy = P_rf.spatial_resolution
        dt = P_rf.temporal_resolution
        for rf in rf_ON, rf_OFF:
            rf.quantize(dx, dy, dt)
        self.rf = {"X_ON": rf_ON, "X_OFF": rf_OFF}

    def get_cache(self, stimulus_id):
        """
        Returns the cached calculated responses due to stimulus corresponding to `stimulus_id`.
        Parameters
        ----------
            stimulus_id : StimulusID
                        The stimulus id of the stimulus for which to return the activities
        Returns
        -------
        Tuple (input_currents, retinal_input)  where input_currents are the currents due to the RFs of the individual RFs and retinal_input is the
        list of frames shown to the retina.
        """

        # If the chache is switched off or we run multiprocess job switch off the cache.
        if self.parameters.cached == False or mozaik.mpi_comm.size > 1:
            return None

        if not os.path.isfile(self.parameters.cache_path + "/" + "stimuli.st"):
            self.cached_stimuli = {}
            return None
        else:
            self.cached_stimuli = load_pickle_crosscompat(self.parameters.cache_path + "/" + "stimuli.st")
            if str(stimulus_id) in self.cached_stimuli:
                z = load_pickle_crosscompat(
                    self.parameters.cache_path
                    + "/"
                    + str(self.cached_stimuli[str(stimulus_id)])
                    + ".st"
                )
                return z
            else:
                return None

    def write_cache(self, stimulus_id, input_currents, retinal_input):
        """
        Stores input currents and the retinal input corresponding to a given stimulus.
        Parameters
        ----------
                stimulus_id : StimulusID
                        The stimulus id of the stimulus for which we will store the input currents
                input_currents : list(ndarray)
                               List containing the input currents that will be injected to the LGN neurons due to the neuron's RFs. One per each LGN neuron.
                retinal_input : list(ndarray)
                              List of 2D arrays containing the frames of luminances that were presented to the retina for the stimulus `stimulus_id`.
        """
        if self.parameters.cached == False:
            return None

        if str(stimulus_id) not in self.cached_stimuli:
            counter = (
                0
                if (len(list(self.cached_stimuli.values())) == 0)
                else max(self.cached_stimuli.values()) + 1
            )

            self.cached_stimuli[str(stimulus_id)] = counter

            logger.debug("Stored spikes to cache...")

            f1 = open(self.parameters.cache_path + "/" + "stimuli.st", "wb")
            f = open(self.parameters.cache_path + "/" + str(counter) + ".st", "wb")
            pickle.dump(self.cached_stimuli, f1)
            pickle.dump((input_currents, retinal_input), f)
            f.close()
            f1.close()

    def process_input(self, visual_space, stimulus, duration=None, offset=0):
        """
        Present a visual stimulus to the model, and create the LGN output
        (relay) neurons.

        Parameters
        ----------
        visual_space : VisualSpace
                     The visual space to which the stimuli are presented.

        stimulus : VisualStimulus
                 The visual stimulus to be shown.

        duration : int (ms)
                 The time for which we will simulate the stimulus

        offset : int(ms)
               The time (in absolute time of the whole simulation) at which the stimulus starts.

        Returns
        -------
        retinal_input : list(ndarray)
                      List of 2D arrays containing the frames of luminances that were presented to the retina.
        """
        logger.debug("Presenting visual stimulus from visual space %s" % visual_space)
        visual_space.set_duration(duration)
        self.input = visual_space
        st = MozaikParametrized.idd(stimulus)
        st.trial = None  # to avoid recalculating RFs response to multiple trials of the same stimulus

        cached = self.get_cache(st)
        print("str(st) ", str(st))
        print("cached ", cached)
        print("cached == None ", cached == None)
        if cached == None:
            print("Generating output spikes...")
            logger.debug("Generating output spikes...")
            # Even if we didn't find the stimulus in cache, we still check if we haven't already presented it during this simulation run.
            # This is mainly to avoid regenerating stimuli for multiple trials.

            if str(st) in self.internal_stimulus_cache:
                print("str(st) in self.internal_stimulus_cache ", str(st) in self.internal_stimulus_cache)
                (input_currents, retinal_input) = self.internal_stimulus_cache[str(st)]
            else:
                (input_currents, retinal_input) = self._calculate_input_currents(visual_space,
                                                                                 duration)
        else:
            print("Retrieved spikes from cache...")
            logger.debug("Retrieved spikes from cache...")
            (input_currents, retinal_input) = cached

        ts = self.model.sim.get_time_step()
        # print("input_currents ", input_currents)
        for rf_type in self.rf_types:
            # break
            # print("length of scs ", len(self.scs[rf_type]))
            # print("first scs ", self.scs[rf_type][0].get_parameters)
            assert isinstance(input_currents[rf_type], list)
            # for i, (lgn_cell, input_current, scs, ncs) in enumerate(
            #        zip(self.sheets[rf_type].pop,
            #            input_currents[rf_type],
            #            self.scs[rf_type],
            #            self.ncs[rf_type])):

            # for i, (lgn_cell, input_current, scs) in enumerate(
            #        zip(self.sheets[rf_type].pop,
            #            input_currents[rf_type],
            #            self.scs[rf_type])):
            for i, (lgn_cell, input_current) in enumerate(
                        zip(self.sheets[rf_type].pop,
                            input_currents[rf_type])):
                # if i == 0:
                #    break
                assert isinstance(input_current, dict)
                t = input_current['times'] + offset
                # t2 = t[::2]  # take every other step
                a = self.parameters.linear_scaler * input_current['amplitudes']
                # a2 = a[::2]  # take every other step
                # scs.set_parameters(times=t, amplitudes=a, copy=False)
                # scs.set_parameters(times=t2, amplitudes=a2)
                if i == 0:
                    print("amplitudes for first ", a)
                    print("times for first", t)
                # scs.set_parameters(times=t, amplitudes=a)
                # lgn_cell.inject(scs)
                t = t[::8]
                a = a[::8]
                scs = self.model.sim.StepCurrentSource(times=t, amplitudes=a)
                lgn_cell.inject(scs)
                # if self.parameters.mpi_reproducible_noise:
                #    print("noisy current modified in process input")
                #    t = numpy.arange(0, duration, ts) + offset
                #    amplitudes = (self.parameters.noise.mean
                #                  + self.parameters.noise.stdev
                #                  * self.ncs_rng[rf_type][i].randn(len(t)))
                #    # ncs.set_parameters(times=t, amplitudes=amplitudes, copy=False)
                #    ncs.set_parameters(times=t, amplitudes=amplitudes)
            # print("first scs 2 ", self.scs[rf_type][0].get_parameters)
            # scs = self.model.sim.StepCurrentSource(times=t[0], amplitudes=[5.0])
            # self.sheets[rf_type].pop.inject(scs)

        self._built = True
        self.write_cache(st, input_currents, retinal_input)
        # also save into internal cache
        self.internal_stimulus_cache[str(st)] = (input_currents, retinal_input)
        # print("scs amplitudes in on pop ", self.scs["X_ON"][1])
        print("retinal_input ", retinal_input)
        print("retinal_input is None ", retinal_input is None)
        return retinal_input

    def process_input2(self, visual_space, stimulus, duration=None, offset=0):
        """
        Present a visual stimulus to the model, and create the LGN output
        (relay) neurons.
        Parameters
        ----------
        visual_space : VisualSpace
                     The visual space to which the stimuli are presented.
        stimulus : VisualStimulus
                 The visual stimulus to be shown.
        duration : int (ms)
                 The time for which we will simulate the stimulus
        offset : int(ms)
               The time (in absolute time of the whole simulation) at which the stimulus starts.
        Returns
        -------
        retinal_input : list(ndarray)
                      List of 2D arrays containing the frames of luminances that were presented to the retina.
        """
        # self.model.sim.set_number_of_neurons_per_core(self.model.sim.Izhikevich, 2047)
        # self.model.sim.set_number_of_neurons_per_core(self.model.sim.IF_cond_exp, 2047)
        logger.debug("Presenting visual stimulus from visual space %s" % visual_space)
        visual_space.set_duration(duration)
        self.input = visual_space
        st = MozaikParametrized.idd(stimulus)
        st.trial = None  # to avoid recalculating RFs response to multiple trials of the same stimulus

        cached = self.get_cache(st)

        if cached == None:
            logger.debug("Generating output spikes...")
            # Even if we didn't find the stimulus in cache, we still check if we haven't already presented it during this simulation run.
            # This is mainly to avoid regenerating stimuli for multiple trials.

            if str(st) in self.internal_stimulus_cache:
                (input_currents, retinal_input) = self.internal_stimulus_cache[str(st)]
            else:
                (input_currents, retinal_input) = self._calculate_input_currents(
                    visual_space, duration
                )
        else:
            logger.debug("Retrieved spikes from cache...")
            (input_currents, retinal_input) = cached

        # print("input_currents ", input_currents)
        # print("input_currents X_ON ", input_currents['X_ON'])
        # print("input_currents X_ON ", input_currents['X_ON'])
        # logger.debug("input_currents X_ON ", input_currents['X_ON'])

        ts = self.model.sim.get_time_step()
        # a = []
        # t = []
        # print("len(input_currents['X_ON'][0]['times'])", len(input_currents['X_ON'][0]['times']))
        # print("len(input_currents['X_OFF'][0]['times'])", len(input_currents['X_OFF'][0]['times']))
        # print("self.sheets[rf_type].pop len ", len(self.sheets['X_ON'].pop))
        # print("input_currents[rf_type] len", len(input_currents['X_ON']))
        # i_offset update and sim.run instead of stepcurrentsource
        # print(self.rf_types[8])  # induce error
        self.sheets['X_ON'].pop.set(i_offset=input_currents['X_ON'][0]['amplitudes'][0])
        self.sheets['X_OFF'].pop.set(i_offset=input_currents['X_OFF'][0]['amplitudes'][0])
        self.model.simulator_time += self.model.sim.run(self.parameters.receptive_field.temporal_resolution)
        for n in range(len(input_currents['X_ON'][0]['times'])):
            print("process input ", n)
            if n == 0:
                break
            for rf_type in self.rf_types:
                # assert isinstance(input_currents[rf_type], list)
                # ts = input_current["times"] + offset
                # ams = self.parameters.linear_scaler * input_current["amplitudes"]
                # print("step current time ", ts)
                # print("step current amplitude ", ams)
                # np = sim.Population(10, sim.SpikeSourceArray(spike_times=t), label="spikes")  # test this
                # sim.Projection(np, self.sheets[rf_type].pop, sim.AllToAllConnector())

                # for i, (lgn_cell, input_current, scs, ncs) in enumerate(
                for i, (lgn_cell, input_current) in enumerate(
                    zip(
                        self.sheets[rf_type].pop,
                        input_currents[rf_type],
                        # self.scs[rf_type],
                        # self.ncs[rf_type]
                    )
                ):
                    # if i == 2:
                    #    break
                    # assert isinstance(input_current, dict)

                    # t = input_current["times"] + offset

                    a = self.parameters.linear_scaler * input_current["amplitudes"]
                    # logger.debug("input_current ", input_current)
                    # print("step current time ", t)
                    # logger.debug("step current time ", t)
                    # print("step current amplitude ", a)
                    # logger.debug("step current amplitude ", a)
                    # print("self.parameters.mpi_reproducible_noise ", self.parameters.mpi_reproducible_noise)

                    # lgn_cell.i_offset = a[0]
                    lgn_cell.i_offset = a[n]
                    # sim.reset ?

                    # scs.set_parameters(times=t, amplitudes=a, copy=False)  # this has to change

                    if False:
                        # if self.parameters.mpi_reproducible_noise:
                        t = numpy.arange(0, duration, ts) + offset
                        amplitudes = self.parameters.noise.mean + self.parameters.noise.stdev * self.ncs_rng[
                            rf_type
                        ][
                            i
                        ].randn(
                            len(t)
                        )
                        ncs.set_parameters(times=t, amplitudes=amplitudes, copy=False)  # this has to change
            # self.model.sim.run(10)
            self.model.simulator_time += self.model.sim.run(self.parameters.receptive_field.temporal_resolution)  # 7.0

        # for debugging/testing, doesn't work with MPI !!!!!!!!!!!!
        # input_current_array = numpy.zeros((self.shape[1], self.shape[0], len(visual_space.time_points(duration))))
        # update_factor = int(visual_space.update_interval/self.parameters.receptive_field.temporal_resolution)
        # logger.debug("input_current_array.shape = %s, update_factor = %d, p.dim = %s" % (input_current_array.shape, update_factor, self.shape))
        # k = 0
        # for i in range(self.shape[1]): # self.sahpe gives (x,y), so self.shape[1] is the height
        #    for j in range(self.shape[0]):
        # where the kernel temporal resolution is finer than the frame update interval,
        # we only keep the current values at the start of each frame
        #        input_current_array[i,j, :] = input_currents['X_ON'][k]['amplitudes'][::update_factor]
        #        k += 1

        # move these to somewhere else (before the loop)?
        # if record() has already been called, setup the recording now
        self._built = True
        self.write_cache(st, input_currents, retinal_input)
        # also save into internal cache
        self.internal_stimulus_cache[str(st)] = (input_currents, retinal_input)

        # return retinal_input, a, t
        return retinal_input

    def provide_null_input(self, visual_space, duration=None, offset=0):
        """
        This function exists for optimization purposes. It is the analog to
        :func:.`mozaik.retinal.SpatioTemporalFilterRetinaLGN.process_input` for the
        special case when blank stimulus is shown.

        Parameters
        ----------
        visual_space : VisualSpace
                     The visual space to which the blank stimulus are presented.

        duration : int (ms)
                 The time for which we will simulate the blank stimulus

        offset : int(ms)
               The time (in absolute time of the whole simulation) at which the stimulus starts.

        Returns
        -------
        retinal_input : list(ndarray)
                      List of 2D arrays containing the frames of luminances that were presented to the retina.
        """
        times = numpy.array([offset,
                             duration - visual_space.update_interval + offset])  # numpy.arange(0, duration, visual_space.update_interval) + offset
        zers = times * 0
        ts = self.model.sim.get_time_step()

        input_cells = OrderedDict()
        for rf_type in self.rf_types:
            input_cells[rf_type] = CellWithReceptiveField(  # self.sheets[rf_type].pop.positions[0][0],
                                                          self.sheets[rf_type].pop.positions[0][0],
                                                          # self.sheets[rf_type].pop.positions[1][0],
                                                          self.sheets[rf_type].pop.positions[0][1],
                                                          self.rf[rf_type],
                                                          self.parameters.gain_control, visual_space)
            input_cells[rf_type].initialize(visual_space.background_luminance, duration)

        for rf_type in self.rf_types:
            # break
            if self.parameters.gain_control.non_linear_gain != None:
                amplitude = self.parameters.linear_scaler * self.parameters.gain_control.non_linear_gain.luminance_gain * numpy.sum(
                    input_cells[rf_type].receptive_field.kernel.flatten()) * visual_space.background_luminance / (
                                        self.parameters.gain_control.non_linear_gain.luminance_scaler * visual_space.background_luminance + 1.0)
            else:
                amplitude = self.parameters.linear_scaler * self.parameters.gain_control.gain * numpy.sum(
                    input_cells[rf_type].receptive_field.kernel.flatten()) * visual_space.background_luminance

            # for i, (scs, ncs) in enumerate(zip(self.scs[rf_type], self.ncs[rf_type])):
            # for i, scs in enumerate(self.scs[rf_type]):
            for lgn_cell in self.sheets[rf_type].pop.all_cells:
                # scs.set_parameters(times=times, amplitudes=zers + amplitude, copy=False)
                # scs.set_parameters(times=times, amplitudes=zers + amplitude)
                scs = self.model.sim.StepCurrentSource(times=times, amplitudes=zers + amplitude)
                lgn_cell.inject(scs)
                # if self.parameters.mpi_reproducible_noise:
                #    print("noisy current modified in null input")
                #    t = numpy.arange(0, duration, ts) + offset
                #    amplitudes = (self.parameters.noise.mean
                #                  + self.parameters.noise.stdev
                #                  * self.ncs_rng[rf_type][i].randn(len(t)))
                #    # ncs.set_parameters(times=t, amplitudes=amplitudes, copy=False)
                #    ncs.set_parameters(times=t, amplitudes=amplitudes)

    def provide_null_input2(self, visual_space, duration=None, offset=0):
        """
        This function exists for optimization purposes. It is the analog to
        :func:.`mozaik.retinal.SpatioTemporalFilterRetinaLGN.process_input` for the
        special case when blank stimulus is shown.
        Parameters
        ----------
        visual_space : VisualSpace
                     The visual space to which the blank stimulus are presented.
        duration : int (ms)
                 The time for which we will simulate the blank stimulus
        offset : int(ms)
               The time (in absolute time of the whole simulation) at which the stimulus starts.
        Returns
        -------
        retinal_input : list(ndarray)
                      List of 2D arrays containing the frames of luminances that were presented to the retina.
        """
        print("XXX provide_null_input XXX ", duration)
        logger.debug("XXX provide_null_input XXX ", duration)
        # self.model.sim.set_number_of_neurons_per_core(self.model.Izhikevich, 2047)
        # self.model.sim.set_number_of_neurons_per_core(self.model.IF_cond_exp, 2047)
        # self.model.sim.set_number_of_neurons_per_core(self.sheets["X_ON"].pop.cellclass, 2047)
        # self.model.sim.set_number_of_neurons_per_core(self.sheets["X_OFF"].pop.cellclass, 2047)
        print("offset ", offset)
        print("visual_space.update_interval ", visual_space.update_interval)
        times = numpy.array(
            [offset, duration - visual_space.update_interval + offset]
        )  # numpy.arange(0, duration, visual_space.update_interval) + offset

        # workaround because of multiple sim runs ?
        # times2 = numpy.array(0, duration - visual_space.update_interval)
        times2 = numpy.array(
            [0, duration - visual_space.update_interval]
        )
        print("times2 ", times2)
        zers = times * 0
        ts = self.model.sim.get_time_step()
        i = len(times)

        input_cells = {}
        for rf_type in self.rf_types:
            input_cells[rf_type] = CellWithReceptiveField(
                # self.sheets[rf_type].pop.positions[0][0],
                self.sheets[rf_type].pop.positions[0][0],
                # self.sheets[rf_type].pop.positions[1][0],
                self.sheets[rf_type].pop.positions[0][1],
                self.rf[rf_type],
                self.parameters.gain_control,
                visual_space
            )
            input_cells[rf_type].initialize(visual_space.background_luminance, duration)
            # if False:
        for n, t in enumerate(times2):
            print("provide null input ", n)
            if n == 1:
                break
            for rf_type in self.rf_types:
                if self.parameters.gain_control.non_linear_gain != None:
                    # print("non_linear_gain != None ")
                    amplitude = (
                        self.parameters.linear_scaler
                        * self.parameters.gain_control.non_linear_gain.luminance_gain
                        * numpy.sum(input_cells[rf_type].receptive_field.kernel.flatten())
                        * visual_space.background_luminance
                        / (
                            self.parameters.gain_control.non_linear_gain.luminance_scaler
                            * visual_space.background_luminance
                            + 1.0
                        )
                    )
                else:
                    amplitude = (
                        self.parameters.linear_scaler
                        * self.parameters.gain_control.gain
                        * numpy.sum(input_cells[rf_type].receptive_field.kernel.flatten())
                        * visual_space.background_luminance
                    )

                print("times ", times)
                print("times len", len(times))
                logger.debug("times ", times)
                print("amplitude ", amplitude)
                # print("self.sheets[rf_type].pop.set(i_offset=amplitude) ", self.sheets[rf_type].pop.get("i_offset"))
                # print("zers + amplitude ", zers + amplitude)
                # a = zers + amplitude
                # self.sheets[rf_type].pop.set(i_offset=a[n])
                self.sheets[rf_type].pop.set(i_offset=amplitude)
                # print("self.sheets[rf_type].pop.set(i_offset=amplitude) ", self.sheets[rf_type].pop.get("i_offset"))
                # self.parameters.mpi_reproducible_noise part ?
                # for i, (scs, ncs) in enumerate(zip(self.scs[rf_type], self.ncs[rf_type])):
                #    print("scs ", scs)
                #    scs.set_parameters(times=times, amplitudes=zers + amplitude, copy=False)  # this has to change
                #    print("self.parameters.mpi_reproducible_noise ", self.parameters.mpi_reproducible_noise)
                #    if self.parameters.mpi_reproducible_noise:
                #        t = numpy.arange(0, duration, ts) + offset
                #        amplitudes = self.parameters.noise.mean + self.parameters.noise.stdev * self.ncs_rng[
                #            rf_type
                #        ][
                #            i
                #        ].randn(
                #            len(t)
                #        )
                #        ncs.set_parameters(times=t, amplitudes=amplitudes, copy=False)
            # find a better way, this works only if the times length is 2
            # self.model.simulator_time += self.model.sim.run(duration / 2)
            # sometimes times[0] is not 0, what then?
            # if t == times[-1]:
            #    self.model.simulator_time += self.model.sim.run(duration - t)
            # else:
            #    self.model.simulator_time += self.model.sim.run(times[n+1] - times[n])
            # self.model.sim.set_number_of_neurons_per_core(self.model.sim.Izhikevich, 2047)
            # self.model.sim.set_number_of_neurons_per_core(self.model.sim.IF_cond_exp, 2047)
            if n == 0:
                print("1. duration - visual_space.update_interval ", duration - visual_space.update_interval)
                self.model.simulator_time += self.model.sim.run(duration - visual_space.update_interval)
            elif n == 1:
                print("2. visual_space.update_interval ", visual_space.update_interval)
                self.model.simulator_time += self.model.sim.run(visual_space.update_interval)
            # self.model.simulator_time += self.model.sim.run(duration / i)
            # self.model.simulator_time += self.model.sim.run(duration)

    def _calculate_input_currents(self, visual_space, duration):
        """
        Calculate the input currents for all cells.
        """
        assert isinstance(visual_space, VisualSpace)
        if duration is None:
            duration = visual_space.get_maximum_duration()

        # create population of CellWithReceptiveFields, setting the receptive
        # field centres based on the size/location of self
        logger.debug("Creating population of `CellWithReceptiveField`s")
        print("Creating population of `CellWithReceptiveField`s")
        input_cells = {}
        # effective_visual_field_width, effective_visual_field_height = self.parameters.size
        # x_values = numpy.linspace(-effective_visual_field_width/2.0, effective_visual_field_width/2.0, self.shape[0])
        # y_values = numpy.linspace(-effective_visual_field_height/2.0, effective_visual_field_height/2.0, self.shape[1])
        for rf_type in self.rf_types:
            input_cells[rf_type] = []
            if hasattr(self.sheets[rf_type].pop, "_mask_local"):
                indices = numpy.nonzero(self.sheets[rf_type].pop._mask_local)[0]
            else:
                indices = numpy.arange(self.sheets[rf_type].pop.size)
            print("indices ", indices)
            print("visual_space ", visual_space)
            for i in indices:
                # for i in range(0,len(self.sheets[rf_type].pop.positions[0])):
                print(self.sheets[rf_type].pop.positions[i][0])
                print(self.sheets[rf_type].pop.positions[i][1])
                print()
                cell = CellWithReceptiveField(
                    # self.sheets[rf_type].pop.positions[0][i],
                    self.sheets[rf_type].pop.positions[i][0],
                    # self.sheets[rf_type].pop.positions[1][i],
                    self.sheets[rf_type].pop.positions[i][1],
                    self.rf[rf_type],
                    self.parameters.gain_control,
                    visual_space
                )
                print("CellWithReceptiveField ", cell)
                cell.initialize(visual_space.background_luminance, duration)
                input_cells[rf_type].append(cell)

        logger.debug("Processing frames")

        t = 0
        retinal_input = []

        if False:
            import threading

            while t < duration:
                t = visual_space.update()
                for rf_type in self.rf_types:
                    threads = []
                    for cell in input_cells[rf_type]:
                        thread = threading.Thread(target=cell.view())
                        thread.start()
                        threads.append(thread)
                        # cell.view()
                    for t in threads:
                        t.join()
        print("duration ", duration)
        print("t ", t)
        while t < duration:
            t = visual_space.update()
            for rf_type in self.rf_types:
                for cell in input_cells[rf_type]:
                    cell.view()
            print("self.model.parameters.store_stimuli ", self.model.parameters.store_stimuli)
            print(self.rf["X_ON"].spatial_resolution)
            if self.model.parameters.store_stimuli == True:
                visual_region = VisualRegion(
                    location_x=0,
                    location_y=0,
                    size_x=self.model.visual_field.size_x,
                    size_y=self.model.visual_field.size_y
                )
                im = visual_space.view(
                    visual_region, pixel_size=self.rf["X_ON"].spatial_resolution
                )
            else:
                print("im = None")
                im = None
            print("im ", im)
            retinal_input.append(im)

        input_currents = {}
        for rf_type in self.rf_types:
            input_currents[rf_type] = [
                cell.response_current() for cell in input_cells[rf_type]
            ]
        print("retinal_input ", retinal_input)
        return (input_currents, retinal_input)
