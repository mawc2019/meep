"""Handling of objective functions and objective quantities."""

import abc
import numpy as np
import meep as mp
from .filter_source import FilteredSource
from .optimization_problem import Grid
from meep.simulation import py_v3_to_vec


class ObjectiveQuantity(abc.ABC):
    """A differentiable objective quantity.

    Attributes:
        sim: the Meep simulation object with which the objective quantity is registered.
        frequencies: the frequencies at which the objective quantity is evaluated.
        num_freq: the number of frequencies at which the objective quantity is evaluated.
    """
    def __init__(self, sim):
        self.sim = sim
        self._eval = None
        self._frequencies = None

    @property
    def frequencies(self):
        return self._frequencies

    @property
    def num_freq(self):
        return len(self.frequencies)

    @abc.abstractmethod
    def __call__(self):
        """Evaluates the objective quantity."""

    @abc.abstractmethod
    def register_monitors(self, frequencies):
        """Registers monitors in the forward simulation."""

    @abc.abstractmethod
    def place_adjoint_source(self, dJ):
        """Places appropriate sources for the adjoint simulation."""

    def get_evaluation(self):
        """Evaluates the objective quantity."""
        if self._eval:
            return self._eval
        else:
            raise RuntimeError(
                'You must first run a forward simulation before requesting the evaluation of an objective quantity.'
            )

    def _adj_src_scale(self, include_resolution=True):
        """Calculates the scale for the adjoint sources."""
        T = self.sim.meep_time()
        dt = self.sim.fields.dt
        src = self._create_time_profile()

        if include_resolution:
            num_dims = self.sim._infer_dimensions(self.sim.k_point)
            dV = 1 / self.sim.resolution**num_dims
        else:
            dV = 1

        iomega = (1.0 - np.exp(-1j * (2 * np.pi * self._frequencies) * dt)) * (
            1.0 / dt
        )  # scaled frequency factor with discrete time derivative fix

        # an ugly way to calcuate the scaled dtft of the forward source
        y = np.array([src.swigobj.current(t, dt)
                      for t in np.arange(0, T, dt)])  # time domain signal
        fwd_dtft = np.matmul(
            np.exp(1j * 2 * np.pi * self._frequencies[:, np.newaxis] *
                   np.arange(y.size) * dt), y) * dt / np.sqrt(
                       2 * np.pi)  # dtft

        # Interestingly, the real parts of the DTFT and fourier transform match, but the imaginary parts are very different...
        #fwd_dtft = src.fourier_transform(src.frequency)
        '''
        For some reason, there seems to be an additional phase
        factor at the center frequency that needs to be applied
        to *all* frequencies...
        '''
        src_center_dtft = np.matmul(
            np.exp(1j * 2 * np.pi * np.array([src.frequency])[:, np.newaxis] *
                   np.arange(y.size) * dt), y) * dt / np.sqrt(2 * np.pi)
        adj_src_phase = np.exp(1j * np.angle(src_center_dtft))

        if self._frequencies.size == 1:
            # Single frequency simulations. We need to drive it with a time profile.
            scale = dV * iomega / fwd_dtft / adj_src_phase  # final scale factor
        else:
            # multi frequency simulations
            scale = dV * iomega / adj_src_phase
        # compensate for the fact that real fields take the real part of the current,
        # which halves the Fourier amplitude at the positive frequency (Re[J] = (J + J*)/2)
        if self.sim.using_real_fields():
            scale *= 2
        return scale

    def _create_time_profile(self, fwidth_frac=0.1):
        """Creates a time domain waveform for normalizing the adjoint source(s).

        For single frequency objective functions, we should generate a guassian pulse with a reasonable
        bandwidth centered at said frequency.

        TODO:
        The user may specify a scalar valued objective function across multiple frequencies (e.g. MSE) in
        which case we should check that all the frequencies fit in the specified bandwidth.
        """
        return mp.GaussianSource(
            np.mean(self._frequencies),
            fwidth=fwidth_frac * np.mean(self._frequencies),
        )


class EigenmodeCoefficient(ObjectiveQuantity):
    """A frequency-dependent eigenmode coefficient.
    Attributes:
        volume: the volume over which the eigenmode coefficient is calculated.
        mode: the eigenmode number.
        forward: whether the forward or backward mode coefficient is returned as
          the result of the evaluation.
        kpoint_func: an optional k-point function to use when evaluating the eigenmode
          coefficient. When specified, this overrides the effect of `forward`.
        kpoint_func_overlap_idx: the index of the mode coefficient to return when
          specifying `kpoint_func`. When specified, this overrides the effect of
          `forward` and should have a value of either 0 or 1.
    """
    def __init__(self,
                 sim,
                 volume,
                 mode,
                 forward=True,
                 kpoint_func=None,
                 kpoint_func_overlap_idx=0,
                 decimation_factor=0,
                 **kwargs):
        super().__init__(sim)
        if kpoint_func_overlap_idx not in [0, 1]:
            raise ValueError(
                '`kpoint_func_overlap_idx` should be either 0 or 1, but got %d'
                % (kpoint_func_overlap_idx, ))
        self.volume = volume
        self.mode = mode
        self.forward = forward
        self.kpoint_func = kpoint_func
        self.kpoint_func_overlap_idx = kpoint_func_overlap_idx
        self.eigenmode_kwargs = kwargs
        self._monitor = None
        self._cscale = None
        self.decimation_factor = decimation_factor

    def register_monitors(self, frequencies):
        self._frequencies = np.asarray(frequencies)
        self._monitor = self.sim.add_mode_monitor(
            frequencies,
            mp.ModeRegion(center=self.volume.center, size=self.volume.size),
            yee_grid=True,
            decimation_factor=self.decimation_factor,
        )
        return self._monitor

    def place_adjoint_source(self, dJ):
        dJ = np.atleast_1d(dJ)
        if dJ.ndim == 2:
            dJ = np.sum(dJ, axis=1)
        time_src = self._create_time_profile()
        da_dE = 0.5 * self._cscale
        scale = self._adj_src_scale()

        if self.kpoint_func:
            eig_kpoint = -1 * self.kpoint_func(time_src.frequency, self.mode)
        else:
            center_frequency = 0.5 * (np.min(self.frequencies) + np.max(
                self.frequencies))
            direction = mp.Vector3(
                *(np.eye(3)[self._monitor.normal_direction] *
                  np.abs(center_frequency)))
            eig_kpoint = -1 * direction if self.forward else direction

        if self._frequencies.size == 1:
            amp = da_dE * dJ * scale
            src = time_src
        else:
            scale = da_dE * dJ * scale
            src = FilteredSource(
                time_src.frequency,
                self._frequencies,
                scale,
                self.sim.fields.dt,
            )
            amp = 1
        source = mp.EigenModeSource(
            src,
            eig_band=self.mode,
            direction=mp.NO_DIRECTION,
            eig_kpoint=eig_kpoint,
            amplitude=amp,
            eig_match_freq=True,
            size=self.volume.size,
            center=self.volume.center,
            **self.eigenmode_kwargs,
        )
        return [source]

    def __call__(self):
        if self.kpoint_func:
            kpoint_func = self.kpoint_func
            overlap_idx = self.kpoint_func_overlap_idx
        else:
            center_frequency = 0.5 * (np.min(self.frequencies) + np.max(
                self.frequencies))
            kpoint = mp.Vector3(*(np.eye(3)[self._monitor.normal_direction] *
                                  np.abs(center_frequency)))
            kpoint_func = lambda *not_used: kpoint if self.forward else -1 * kpoint
            overlap_idx = 0
        ob = self.sim.get_eigenmode_coefficients(
            self._monitor,
            [self.mode],
            direction=mp.NO_DIRECTION,
            kpoint_func=kpoint_func,
            **self.eigenmode_kwargs,
        )
        overlaps = ob.alpha.squeeze(axis=0)
        assert overlaps.ndim == 2
        self._eval = overlaps[:, overlap_idx]
        self._cscale = ob.cscale
        return self._eval

class FourierFields(ObjectiveQuantity):
    def __init__(self,sim,volume, component, yee_grid):
        #self.sim = sim
        super().__init__(sim)
        self.volume = sim._fit_volume_to_simulation(volume)
        self.eval = None
        self.component = component
        self.yee_grid = yee_grid
        return

    def register_monitors(self,frequencies):
        self._frequencies = np.asarray(frequencies)
        #self.num_freq = len(self._frequencies)
        self._monitor = self.sim.add_dft_fields([self.component], self._frequencies, where=self.volume, yee_grid=self.yee_grid)
        return self._monitor

    def place_adjoint_source(self,dJ):
        dt = self.sim.fields.dt # the timestep size from sim.fields.dt of the forward sim
        self.sources = []
        dJ = dJ.flatten()
        min_max_corners = self.sim.fields.get_corners(self._monitor.swigobj, self.component) # get the ivec values of the corners
        self.all_fouriersrcdata = self._monitor.swigobj.fourier_sourcedata(self.volume.swigobj, min_max_corners, dJ)

        for near_data in self.all_fouriersrcdata:
            amp_arr = np.array(near_data.amp_arr).reshape(-1, self.num_freq)
            scale = amp_arr * self._adj_src_scale(include_resolution=False) #adj_src_scale(self, dt, include_resolution=False)
            
            if self.num_freq == 1:
                self.sources += [mp.IndexedSource(self.time_src, near_data, scale[:,0])]
            else:
                src = FilteredSource(self.time_src.frequency,self._frequencies,scale,dt)
                (num_basis, num_pts) = src.nodes.shape
                for basis_i in range(num_basis):
                    self.sources += [mp.IndexedSource(src.time_src_bf[basis_i], near_data, src.nodes[basis_i])]

        return self.sources
    
    def __call__(self):
        self._eval = np.array([self.sim.get_dft_array(self._monitor, self.component, i) for i in range(self.num_freq)])
        self.time_src = self._create_time_profile()
        return self._eval


class Near2FarFields(ObjectiveQuantity):
    def __init__(self, sim, Near2FarRegions, far_pts, decimation_factor=0):
        super().__init__(sim)
        self.Near2FarRegions = Near2FarRegions
        self.far_pts = far_pts  #list of far pts
        self._nfar_pts = len(far_pts)
        self.decimation_factor = decimation_factor

    def register_monitors(self, frequencies):
        self._frequencies = np.asarray(frequencies)
        self._monitor = self.sim.add_near2far(
            self._frequencies,
            *self.Near2FarRegions,
            decimation_factor=self.decimation_factor,
        )
        return self._monitor

    def place_adjoint_source(self, dJ):
        time_src = self._create_time_profile()
        sources = []
        if dJ.ndim == 4:
            dJ = np.sum(dJ, axis=0)
        dJ = dJ.flatten()
        farpt_list = np.array([list(pi) for pi in self.far_pts]).flatten()
        far_pt0 = self.far_pts[0]
        far_pt_vec = py_v3_to_vec(
            self.sim.dimensions,
            far_pt0,
            self.sim.is_cylindrical,
        )

        all_nearsrcdata = self._monitor.swigobj.near_sourcedata(
            far_pt_vec, farpt_list, self._nfar_pts, dJ)
        for near_data in all_nearsrcdata:
            cur_comp = near_data.near_fd_comp
            amp_arr = np.array(near_data.amp_arr).reshape(-1, self.num_freq)
            scale = amp_arr * self._adj_src_scale(include_resolution=False)

            if self.num_freq == 1:
                sources += [mp.IndexedSource(time_src, near_data, scale[:, 0])]
            else:
                src = FilteredSource(
                    time_src.frequency,
                    self._frequencies,
                    scale,
                    self.sim.fields.dt,
                )
                (num_basis, num_pts) = src.nodes.shape
                for basis_i in range(num_basis):
                    sources += [
                        mp.IndexedSource(
                            src.time_src_bf[basis_i],
                            near_data,
                            src.nodes[basis_i],
                        )
                    ]

        return sources

    def __call__(self):
        self._eval = np.array([
            self.sim.get_farfield(self._monitor, far_pt)
            for far_pt in self.far_pts
        ]).reshape((self._nfar_pts, self.num_freq, 6))
        return self._eval
