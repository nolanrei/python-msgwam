from __future__ import annotations
from abc import ABC, abstractmethod
from copy import copy
from typing import Any, Optional

import numpy as np
import scipy as sp
import xarray as xr

from . import config
from .mean import MeanFlow
from .rays import RayCollection

class Integrator(ABC):
    def __init__(self) -> None:
        """
        Initialize the Integrator and the arrays that will hold snapshots of the
        mean flow and the waves when the system is integrated.
        """

        mean = MeanFlow()
        rays = RayCollection(mean)

        self.time = config.dt * np.arange(config.n_t_max)
        if not config.interactive_mean:
            with xr.open_dataset(config.mean_file) as ds:
                ds = ds.interp(time=self.time)
                self.prescribed_u = ds['u'].values
                self.prescribed_v = ds['v'].values

            mean.u = self.prescribed_u[0]
            mean.v = self.prescribed_v[0]
        
        self.int_mean = [mean]
        self.int_rays = [rays]
        self.int_pmf = [self.center(mean.pmf(rays))]
        
    @abstractmethod
    def step(
        self,
        mean: MeanFlow,
        rays: RayCollection
    ) -> tuple[MeanFlow, RayCollection]:
        """
        Advance the state of the system by one time step. Should be implemented
        by every Integrator subclass.

        Parameters
        ----------
        mean
            Current MeanFlow.
        rays
            Current RayCollection.

        Returns
        -------
        MeanFlow
            Updated MeanFlow.
        RayCollection
            Updated RayCollection.

        """

        pass

    def integrate(self) -> Integrator:
        """
        Integrate the system over the time interval specified in config.

        Returns
        -------
        Integrator
            The integrated system.

        """

        mean = self.int_mean[0]
        rays = self.int_rays[0]

        for i in range(1, config.n_t_max):
            rays.check_boundaries(mean)
            mean, rays = self.step(mean, rays)
            
            if not config.interactive_mean:
                mean.u = self.prescribed_u[i]
                mean.v = self.prescribed_v[i]

            if not config.saturate_online:
                max_dens = rays.max_dens(mean)
                idx = rays.dens > max_dens
                rays.dens[idx] = max_dens[idx]

            if i % config.n_skip == 0:
                self.int_mean.append(mean)
                self.int_rays.append(rays)
                self.int_pmf.append(self.center(mean.pmf(rays)))

        return self

    def center(self, pmf: np.ndarray) -> np.ndarray:
        r_from = self.int_mean[0].r_faces
        r_to = self.int_mean[0].r_centers

        return np.vstack((
            np.interp(r_to, r_from, pmf[0]),
            np.interp(r_to, r_from, pmf[1])
        ))

    def to_dataset(self) -> xr.Dataset:
        """
        Return a Dataset holding the data from the integrated system.
        """

        data: dict[str, Any] = {
            'time' : self.time[::config.n_skip],
            'nray' : np.arange(config.n_ray_max),
            'grid' : self.int_mean[0].r_centers
        }
        
        for name in ['u', 'v']:
            stacked = np.vstack([getattr(mean, name) for mean in self.int_mean])
            data[name] = (('time', 'grid'), stacked)

        for name in RayCollection.props:
            stacked = np.vstack([getattr(rays, name) for rays in self.int_rays])
            data[name] = (('time', 'nray'), stacked)

        stacked = np.stack(self.int_pmf).transpose(1, 0, 2)
        data['pmf_u'] = (('time', 'grid'), stacked[0])
        data['pmf_v'] = (('time', 'grid'), stacked[1])

        return xr.Dataset(data)
    
class RK3Integrator(Integrator):
    aa = [0, -5 / 9, -153 / 128]
    bb = [1 / 3, 15 / 16, 8 / 15]

    def step(
        self,
        mean: MeanFlow,
        rays: RayCollection
    ) -> tuple[MeanFlow, RayCollection]:
        """Take an RK3 step."""

        p: float | np.ndarray = 0
        q: float | np.ndarray = 0

        for a, b in zip(self.aa, self.bb):
            dmean_dt = mean.dmean_dt(rays)
            drays_dt = rays.drays_dt(mean)

            p = config.dt * dmean_dt + a * p
            q = config.dt * drays_dt + a * q

            mean = mean + b * p
            rays = rays + b * q

        return mean, rays
    
class SBDF2Integrator(Integrator):
    def __init__(self) -> None:
        super().__init__()

        m = config.n_grid - 1
        D = np.zeros((m, m))

        for i in range(1, m - 1):
            D[i, (i - 1):(i + 2)] = np.array([1, -2, 1])

        D[0, :2] = np.array([-6, 2])
        D[-1, -2:] = np.array([2, -6])
        D = config.nu * D / self.int_mean[0].dr ** 2

        self.A = sp.linalg.lu_factor(np.eye(m) - config.dt * D)
        self.B = sp.linalg.lu_factor((3 / 2) * np.eye(m) - config.dt * D)

        self.last_u = None
        self.last_v = None
        self.last_rays = None

        self.last_du_dt = None
        self.last_dv_dt = None
        self.last_drays_dt = None
    
    def step(
        self,
        mean: MeanFlow,
        rays: RayCollection
    ) -> tuple[MeanFlow, RayCollection]:
        """Advance the mean flow and rays with an SBDF2 step."""

        du_dt, dv_dt = mean.dmean_dt(rays)
        drays_dt = rays.drays_dt(mean)

        if self.last_u is None:
            mean = copy(mean)
            mean.u = sp.linalg.lu_solve(self.A, mean.u + config.dt * du_dt)
            mean.v = sp.linalg.lu_solve(self.A, mean.v + config.dt * dv_dt)
            rays = rays + config.dt * drays_dt

        else:
            last_u = self.last_u
            last_v = self.last_v
            last_rays = self.last_rays

            last_du_dt = self.last_du_dt
            last_dv_dt = self.last_dv_dt
            last_drays_dt = self.last_drays_dt

            idx = np.isnan(last_drays_dt[0])
            last_rays[:, idx] = rays.data[:, idx]
            last_drays_dt[:, idx] = 0

            mean, rays = copy(mean), copy(rays)
            mean.u = self.lhs(mean.u, last_u, du_dt, last_du_dt, self.B)
            mean.v = self.lhs(mean.v, last_v, dv_dt, last_dv_dt, self.B)
            rays.data = self.lhs(rays.data, last_rays, drays_dt, last_drays_dt)

        self.last_u = mean.u
        self.last_v = mean.v
        self.last_rays = rays.data

        self.last_du_dt = du_dt
        self.last_dv_dt = dv_dt
        self.last_drays_dt = drays_dt

        return mean, rays
    
    @staticmethod
    def lhs(
        curr: np.ndarray,
        last: np.ndarray,
        dcurr_dt: np.ndarray,
        dlast_dt: np.ndarray,
        B: Optional[tuple]=None
    ) -> None:
        """
        Compute the left-hand side of the SBDF2 discretization.

        Parameters
        ----------
        curr
            Current values.
        last
            Previous time step values.
        dcurr_dt
            Current time tendency.
        dlast_dt
            Previous time step tendency.
        B, optional
            Matrix to apply to the left-hand side of the SBDF2 equation,
            containing both the identity term and the stiff term. If None, the
            stiff term is assumed to be zero (equivalent to passing in 3 / 2
            times the identity as B)l

        Returns
        -------
        np.ndarray
            Next time step values.

        """

        rhs = 2 * curr - 0.5 * last + config.dt * (2 * dcurr_dt - dlast_dt)

        if B is None:
            return (2 / 3) * rhs
        
        return sp.linalg.lu_solve(B, rhs)
