"""
docstring goes here
"""
from neo.core.segment import Segment
import numpy
import cPickle
import quantities as qt


class MozaikSegment(Segment):
        """
        This class extends Neo segment with several convenience functions.

        The most important function is that it allows lazy loading of the data.

        It should be moved to datastore.py once the NeoNeurotoolsWrapper is
        obsolete and this file should be discarded.
        """

        def __init__(self, segment, identifier):
            """
            """
            self.init = True
            Segment.__init__(self, name=segment.name,
                             description=segment.description,
                             file_origin=segment.file_origin,
                             file_datetime=segment.file_datetime,
                             rec_datetime=segment.rec_datetime,
                             index=segment.index)

            self.annotations = segment.annotations
            self.identifier = identifier
            # indicates whether the segment has been fully loaded
            self.full = False

        def get_spiketrains(self):
            if not self.full:
                self.load_full()
            return self._spiketrains

        def set_spiketrains(self, s):
            if self.init:
                self.init = False
                return
            raise ValueError('The spiketrains property should never be directly set in MozaikSegment!!!')

        spiketrains = property(get_spiketrains, set_spiketrains)

        def get_spiketrain(self, neuron_id):
            """
            Returns a spiktrain or a list of spike train corresponding to id(s) in neuron_id
            """
            ids = [s.annotations['source_id'] for s in self.spiketrains]
            if isinstance(neuron_id,list) or isinstance(neuron_id,numpy.ndarray):
              return [self.spiketrains[ids.index(i)] for i in neuron_id]
            else:
              return self.spiketrains[ids.index(neuron_id)]

        def get_vm(self, neuron_id):
            if not self.full:
                self.load_full()

            for a in self.analogsignalarrays:
                if a.name == 'v':
                    return a[:, a.annotations['source_ids'].tolist().index(neuron_id)]

        def get_esyn(self,neuron_id):
            if not self.full:
                self.load_full()
            for a in self.analogsignalarrays:
                if a.name == 'gsyn_exc':
                    return a[:, a.annotations['source_ids'].tolist().index(neuron_id)]

        def get_isyn(self,neuron_id):
            if not self.full:
                self.load_full()
            for a in self.analogsignalarrays:
                if a.name == 'gsyn_inh':
                    return a[:, a.annotations['source_ids'].tolist().index(neuron_id)]

        def load_full(self):
            """
            Load the full version of the Segment and set self.full to True.
            """
            pass

        def neuron_num(self):
            """
            Return number of STORED neurons in the Segment.
            """
            return len(self.spiketrains)
        
        def get_stored_isyn_ids(self):
            if not self.full:
                self.load_full()
            for a in self.analogsignalarrays:
                if a.name == 'gsyn_inh':
                   return a.annotations['source_ids']
        
        def get_stored_esyn_ids(self):
            if not self.full:
                self.load_full()
            for a in self.analogsignalarrays:
                if a.name == 'gsyn_exc':
                   return a.annotations['source_ids']

        def get_stored_vm_ids(self):
            if not self.full:
                self.load_full()
            for a in self.analogsignalarrays:
                if a.name == 'v':
                   return a.annotations['source_ids']

        def get_stored_spike_train_ids(self):
            if not self.full:
                self.load_full()
            return [s.annotations['source_id'] for s in self.spiketrains]

        def mean_rates(self):
            """
            Returns the mean rates of the spiketrains in spikes/s
            """
            return [len(s)/(s.t_stop.rescale(qt.s).magnitude-s.t_start.rescale(qt.s).magnitude) for s in self.spiketrains]

        def isi(self):
            """
            Return an array containing arrays (one per each neurons) with the inter-spike intervals of the SpikeTrain objects.
            """
            return [numpy.diff(s) for s in self.spiketrains]

        def cv_isi(self):
            """
            Return array with the coefficient of variation of the isis, one per each neuron.
            
            cv_isi is the ratio between the standard deviation and the mean of the ISI
            The irregularity of individual spike trains is measured by the squared
            coefficient of variation of the corresponding inter-spike interval (ISI)
            distribution.
            In point processes, low values reflect more regular spiking, a
            clock-like pattern yields CV2= 0. On the other hand, CV2 = 1 indicates
            Poisson-type behavior. As a measure for irregularity in the network one
            can use the average irregularity across all neurons.
            
            http://en.wikipedia.org/wiki/Coefficient_of_variation
            """
            isi = self.isi()
            cv_isi = []
            for _isi in isi:
                if len(_isi) > 0:
                    cv_isi.append(numpy.std(_isi)/numpy.mean(_isi))
                else:
                    cv_isi.append(None)
            return cv_isi

"""
This is a Mozaik wrapper of neo segment, that enables pickling and lazy loading.
"""    

class PickledDataStoreNeoWrapper(MozaikSegment):
        def __init__(self, segment, identifier, datastore_path):
            MozaikSegment.__init__(self, segment, identifier)
            self.datastore_path = datastore_path

        def load_full(self):
            f = open(self.datastore_path + '/' + self.identifier + ".pickle", 'rb')
            s = cPickle.load(f)
            f.close()
            self._spiketrains = s.spiketrains
            self.analogsignalarrays = s.analogsignalarrays
            self.full = True

        def __getstate__(self):
            flag = self.full
            self.full = False
            result = self.__dict__.copy()
            if flag:
                del result['_spiketrains']
                del result['analogsignalarrays']
            return result
        
        def release(self):
            self.full = False
            del self._spiketrains
            del self.analogsignalarrays


def spike_dic_to_list(d):
    sp = []
    for k in d.keys():
        for z in d[k]:
            sp.append([k, z])
    if len(sp) == 0:
        return sp
    sp = numpy.array(sp)
    return sp[sp[:, 1].argsort(), :]
