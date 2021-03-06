from __future__ import (print_function, division, unicode_literals)
from .backports import *
from .imports import (require,
    np, warnings, datetime, piexif, PIL, scipy, shutil, os, matplotlib, copy,
    osgeo, collections, pandas, sys, sharedmem)
from . import (helpers, config)

class Camera(object):
    """
    Distorted camera model.

    A `Camera` converts between 3-D world coordinates and their corresponding 2-D image coordinates.

    By default, cameras are initialized at the origin (0, 0, 0), parallel with the horizon (xy-plane), and pointed north (+y).
    All attributes are coerced to `numpy.ndarray` during initialization or when individually set.
    The focal length in pixels (:attr:`f`) is calculated from :attr:`fmm` and :attr:`sensorsz` if both are provided.
    The principal point offset in pixels (:attr:`c`) is calculated from :attr:`cmm` and :attr:`sensorsz` if both are provided.
    If :attr:`vector` is provided, all arguments are ignored except :attr:`sensorsz`,
    which is saved for later calculation of :attr:`fmm` and :attr:`cmm`.

    Attributes:
        vector (numpy.ndarray): Vector of the core camera attributes
            (:attr:`xyz`, :attr:`viewdir`, :attr:`imgsz`, :attr:`f`, :attr:`c`, :attr:`k`, :attr:`p`)
        xyz (numpy.ndarray): Position in world coordinates (x, y, z)
        viewdir (numpy.ndarray): View direction in degrees (yaw, pitch, roll)

            - yaw: clockwise rotation about z-axis (0 = look north)
            - pitch: rotation from horizon (+ look up, - look down)
            - roll: rotation about optical axis (+ down right, - down left, from behind)

        imgsz (numpy.ndarray): Image size in pixels (nx, ny)
        f (numpy.ndarray): Focal length in pixels (fx, fy)
        c (numpy.ndarray): Principal point offset from the image center in pixels (dx, dy)
        k (numpy.ndarray): Radial distortion coefficients (k1, ..., k6)
        p (numpy.ndarray): Tangential distortion coefficients (p1, p2)
        sensorsz (numpy.ndarray): Sensor size in millimeters (nx, ny)
        fmm (numpy.ndarray): Focal length in millimeters (fx, fy).
            Computed from :attr:`f` and :attr:`sensorsz` (if set).
        cmm (numpy.ndarray): Principal point offset from the image center in millimeters (dx, dy).
            Computed from :attr:`c` and :attr:`sensorsz` (if set).
        R (numpy.ndarray): Rotation matrix equivalent of :attr:`viewdir` (3, 3).
            Assumes an initial camera orientation with +z pointing up, +x east, and +y north.
        Rprime (numpy.ndarray): Derivative of :attr:`R` with respect to :attr:`viewdir`.
            Used for fast Jacobian (gradient) calculations by :class:`optimize.ObserverCameras`.
        original_vector (numpy.ndarray): Value of :attr:`vector` when first initialized
        cameraMatrix (numpy.ndarray): Camera matrix in OpenCV format
        distCoeffs (numpy.ndarray): Distortion coefficients (:attr:`k`, :attr:`p`) in OpenCV format
    """

    def __init__(self, vector=None, xyz=(0, 0, 0), viewdir=(0, 0, 0),
        imgsz=(100, 100), f=(100, 100), c=(0, 0), k=(0, 0, 0, 0, 0, 0), p=(0, 0),
        sensorsz=None, fmm=None, cmm=None):
        self.vector = np.full(20, np.nan, dtype=float)
        self.sensorsz = sensorsz
        if vector is not None:
            self.vector = np.asarray(vector, dtype=float)[0:20]
        else:
            self.xyz = xyz
            self.viewdir = viewdir
            self.imgsz = imgsz
            if (fmm is not None or cmm is not None) and sensorsz is None:
                raise ValueError("'fmm' or 'cmm' provided without 'sensorsz'")
            if sensorsz is not None and fmm is not None:
                self.f = helpers.format_list(fmm, length=2) * self.imgsz / self.sensorsz
            else:
                self.f = f
            if sensorsz is not None and cmm is not None:
                self.c = helpers.format_list(cmm, length=2) * self.imgsz / self.sensorsz
            else:
                self.c = c
            self.k = k
            self.p = p
        self.original_vector = self.vector.copy()

    # ---- Properties (dependent) ----

    @property
    def xyz(self):
        return self.vector[0:3]

    @xyz.setter
    def xyz(self, value):
        self.vector[0:3] = helpers.format_list(value, length=3, default=0)

    @property
    def viewdir(self):
        return self.vector[3:6]

    @viewdir.setter
    def viewdir(self, value):
        self.vector[3:6] = helpers.format_list(value, length=3, default=0)

    @property
    def imgsz(self):
        return self.vector[6:8]

    @imgsz.setter
    def imgsz(self, value):
        self.vector[6:8] = helpers.format_list(value, length=2)

    @property
    def f(self):
        return self.vector[8:10]

    @f.setter
    def f(self, value):
        self.vector[8:10] = helpers.format_list(value, length=2)

    @property
    def c(self):
        return self.vector[10:12]

    @c.setter
    def c(self, value):
        self.vector[10:12] = helpers.format_list(value, length=2, default=0)

    @property
    def k(self):
        return self.vector[12:18]

    @k.setter
    def k(self, value):
        self.vector[12:18] = helpers.format_list(value, length=6, default=0)

    @property
    def p(self):
        return self.vector[18:20]

    @p.setter
    def p(self, value):
        self.vector[18:20] = helpers.format_list(value, length=2, default=0)

    @property
    def sensorsz(self):
        if self._sensorsz is not None:
            return self._sensorsz
        else:
            return np.full(2, np.nan)

    @sensorsz.setter
    def sensorsz(self, value):
        if value is not None:
            value = np.array(helpers.format_list(value, length=2), dtype=float)
        self._sensorsz = value

    @property
    def fmm(self):
        return self.f * self.sensorsz / self.imgsz

    @property
    def cmm(self):
        return self.c * self.sensorsz / self.imgsz

    @property
    def R(self):
        # Initial rotations of camera reference frame
        # (camera +z pointing up, with +x east and +y north)
        # Point camera north: -90 deg counterclockwise rotation about x-axis
        #   ri = [1 0 0; 0 cosd(-90) sind(-90); 0 -sind(-90) cosd(-90)];
        # (camera +z now pointing north, with +x east and +y down)
        # yaw: counterclockwise rotation about y-axis (relative to north, from above: +cw, - ccw)
        #   ry = [C1 0 -S1; 0 1 0; S1 0 C1];
        # pitch: counterclockwise rotation about x-axis (relative to horizon: + up, - down)
        #   rp = [1 0 0; 0 C2 S2; 0 -S2 C2];
        # roll: counterclockwise rotation about z-axis (from behind camera: + ccw, - cw)
        #   rr = [C3 S3 0; -S3 C3 0; 0 0 1];
        # Apply all rotations in order
        #   R = rr * rp * ry * ri;
        radians = np.deg2rad(self.viewdir)
        C = np.cos(radians)
        S = np.sin(radians)
        return np.array([
            [C[0] * C[2] + S[0] * S[1] * S[2],  C[0] * S[1] * S[2] - C[2] * S[0], -C[1] * S[2]],
            [C[2] * S[0] * S[1] - C[0] * S[2],  S[0] * S[2] + C[0] * C[2] * S[1], -C[1] * C[2]],
            [C[1] * S[0]                     ,  C[0] * C[1]                     ,  S[1]       ]
        ])

    @property
    def Rprime(self):
        radians = np.deg2rad(self.viewdir)
        C = np.cos(radians)
        S = np.sin(radians)
        Rprime = np.stack((
            [[ C[0] * S[1] * S[2] - S[0] * C[2],  S[0] * S[2] + C[0] * S[1] * C[2],  C[0] * C[1]],
             [-S[0] * S[1] * S[2] - C[0] * C[2],  C[0] * S[2] - S[0] * S[1] * C[2], -S[0] * C[1]],
             [ 0                               ,  0                               ,  0]],
            [[ S[0] * C[1] * S[2]              ,  S[0] * C[1] * C[2]              , -S[0] * S[1]],
             [ C[0] * C[1] * S[2]              ,  C[0] * C[1] * C[2]              , -C[0] * S[1]],
             [ S[1] * S[2]                     ,  S[1] * C[2]                     ,  C[1]]],
            [[ S[0] * S[1] * C[2] - C[0] * S[2], -S[0] * S[1] * S[2] - C[0] * C[2],  0],
             [ S[0] * S[2] + C[0] * S[1] * C[2],  S[0] * C[2] - C[0] * S[1] * S[2],  0],
             [-C[1] * C[2]                     ,  C[1] * S[2]                     ,  0]]
        ), axis=1)
        return Rprime * (np.pi / 180)

    @property
    def original_imgsz(self):
        return self.original_vector[6:8]

    @property
    def cameraMatrix(self):
        return np.array([
            [self.f[0], 0, self.c[0] + self.imgsz[0] / 2],
            [0, self.f[1], self.c[1] + self.imgsz[1] / 2],
            [0, 0, 1]])

    @property
    def distCoeffs(self):
        return np.hstack((self.k[0:2], self.p[0:2], self.k[2:]))

    @property
    def shape(self):
        return int(self.imgsz[1]), int(self.imgsz[0])

    # ----- Methods (class) ----

    @classmethod
    def read(cls, path, **kwargs):
        """
        Read Camera from JSON.

        See :meth:`write` for the reverse.

        Arguments:
            path (str): Path to JSON file
            **kwargs: Additional parameters passed to :meth:`Camera`.
                These override any parameters read from **path**.

        Returns:
            A :class:`Camera` object
        """
        json_args = helpers.read_json(path)
        for key in json_args.keys():
            # Conversion to float converts None to nan
            value = np.array(json_args[key], dtype=float)
            if np.isnan(value).all():
                value = None
            json_args[key] = value
        args = helpers.merge_dicts(json_args, kwargs)
        return cls(**args)

    # ---- Methods (static) ----

    @staticmethod
    def get_sensor_size(make, model):
        """
        Return the nominal sensor size of a digital camera model.

        Data is from Digital Photography Review (https://dpreview.com) reviews
        and their article https://dpreview.com/articles/8095816568/sensorsizes.

        Arguments:
            make (str): Camera make (see :attr:`Exif.make`)
            model (str): Camera model (see :attr:`Exif.model`)

        Returns:
            tuple: Sensor size in millimeters (nx, ny)
        """
        sensor_sizes = {
            'NIKON CORPORATION NIKON D2X': (23.7, 15.7), # https://www.dpreview.com/reviews/nikond2x/2
            'NIKON CORPORATION NIKON D200': (23.6, 15.8), # https://www.dpreview.com/reviews/nikond200/2
            'NIKON CORPORATION NIKON D300S': (23.6, 15.8), # https://www.dpreview.com/reviews/nikond300s/2
            'NIKON E8700': (8.8, 6.6), # https://www.dpreview.com/reviews/nikoncp8700/2
            'Canon Canon EOS 20D': (22.5, 15.0), # https://www.dpreview.com/reviews/canoneos20d/2
            'Canon Canon EOS 40D': (22.2, 14.8), # https://www.dpreview.com/reviews/canoneos40d/2
        }
        make_model = make.strip() + " " + model.strip()
        if make_model in sensor_sizes:
            return sensor_sizes[make_model]
        else:
            raise KeyError('No sensor size found for: ' + make_model)

    @staticmethod
    def get_scale_from_size(old_size, new_size):
        """
        Return the scale factor that achieves a target image size.

        Arguments:
            old_size (iterable of :obj:`int`): Initial image size (nx, ny)
            new_size (iterable of :obj:`int`): Target image size (nx, ny)

        Returns:
            float: Scale factor, or `None` if **new_size** cannot
            be achieved exactly
        """
        if all(new_size == old_size):
            return 1.0
        scale_bounds = new_size / old_size
        if scale_bounds[0] == scale_bounds[1]:
            return scale_bounds[0]
        def err(scale):
            return np.sum(np.abs(np.floor(scale * old_size + 0.5) - new_size))
        fit = scipy.optimize.minimize(err, x0=scale_bounds.mean(), bounds=[scale_bounds])
        if err(fit['x']) == 0:
            return fit['x']
        else:
            return None

    # ---- Methods (public) ----

    def copy(self):
        """
        Return a copy of this camera.

        The :attr:`original_vector` of the new :class:`Camera` object is set to
        the current value of :attr:`vector`.

        Returns:
            A :class:`Camera` object
        """
        return Camera(vector=self.vector.copy(), sensorsz=self.sensorsz)

    def reset(self):
        """
        Reset core attributes to their original values.

        :attr:`vector` is reset to the value of :attr:`original_vector`.
        """
        self.vector = self.original_vector.copy()

    def as_dict(self, attributes=None):
        """
        Return this camera as a dictionary.

        Arguments:
            attributes (iterable of :obj:`str`): Attributes to include.
                If `None`, defaults to the core attributes
                (:attr:`xyz`, :attr:`viewdir`, :attr:`imgsz`, :attr:`f`, :attr:`c`, :attr:`k`, :attr:`p`).

        Returns:
            dict: Attribute names and values
        """
        if attributes is None:
            attributes = ('xyz', 'viewdir', 'imgsz', 'f', 'c', 'k', 'p')
        return {name: list(getattr(self, name))
            for name in attributes if hasattr(self, name)}

    def write(self, path=None, attributes=None, **kwargs):
        """
        Write or return this camera as JSON.

        See :meth:`read` for the reverse.

        Arguments:
            path (str): Path of file to write to.
                If `None`, a JSON-formatted string is returned.
            attributes (:obj:`list` of :obj:`str`): Attributes to include.
                If `None`, defaults to the core attributes
                (:attr:`xyz`, :attr:`viewdir`, :attr:`imgsz`, :attr:`f`, :attr:`c`, :attr:`k`, :attr:`p`).
            **kwargs: Additional arguments to :func:`helpers.write_json()`

        Returns:
            str: Attribute names and values as a JSON-formatted string,
            or `None` if **path** is specified.
        """
        obj = self.as_dict(attributes=attributes)
        return helpers.write_json(obj, path=path, **kwargs)

    def normal(self, sigma):
        """
        Return a new camera sampled from a normal distribution centered on this
        camera.

        Arguments:
            sigma (:class:`Camera` or :obj:`dict`):

        Returns:
            A :class:`Camera` object
        """
        if isinstance(sigma, self.__class__):
            sigma = sigma.vector
        if isinstance(sigma, dict):
            mean = self.as_dict()
            args = {key: np.add(mean[key],
                np.random.normal(scale=sigma[key]) if sigma.get(key) else 0)
                for key in mean}
            if 'f' in sigma:
                args.pop('fmm', None)
            if 'c' in sigma:
                args.pop('cmm', None)
        else:
            args = {'vector': self.vector + np.random.normal(scale=sigma)}
        return self.__class__(**args)

    def idealize(self):
        """
        Set distortion to zero.

        Radial distortion (`k`), tangential distortion (`p`), and principal point offset (`c`) are set to zero.
        """
        self.k = np.zeros(6, dtype=float)
        self.p = np.zeros(2, dtype=float)
        self.c = np.zeros(2, dtype=float)

    def resize(self, size=1, force=False):
        """
        Resize the camera.

        Image size (`imgsz`), focal length (`f`), and principal point offset (`c`)
        are scaled accordingly.

        Arguments:
            size: Scale factor relative to original size (float)
                or target image size (iterable)
            force (bool): Whether to use `size` even if it does not preserve
                the original aspect ratio
        """
        scale1d = np.atleast_1d(size)
        if len(scale1d) > 1 and force:
            # Use target size without checking for scalar scale factor
            new_size = scale1d
        else:
            if len(scale1d) > 1:
                # Compute scalar scale factor (if one exists)
                scale1d = Camera.get_scale_from_size(self.original_imgsz, scale1d)
                if scale1d is None:
                    raise ValueError('Target size does not preserve original aspect ratio')
            new_size = np.floor(scale1d * self.original_imgsz + 0.5)
        scale2d = new_size / self.imgsz
        self.imgsz = np.round(new_size) # ensure whole numbers
        self.f *= scale2d
        self.c *= scale2d

    def project(self, xyz, directions=False, correction=False,
        return_depth=False):
        """
        Project world coordinates to image coordinates.

        Arguments:
            xyz (array): World coordinates (n, 3)
            directions (bool): Whether `xyz` are absolute coordinates (False)
                or ray directions (True)
            correction: Arguments to `helpers.elevation_corrections()` (dict),
                `True` for default arguments, or `None` or `False` to skip.
                Only applies if `directions` is `False`.
            return_depth (bool): Whether to return the distance of each point
                along the camera's optical axis.

        Returns:
            array: Image coordinates (n, 2)
            array (optional): Point depth (n, )
        """
        xy = self._world2camera(xyz, directions=directions,
            correction=correction, return_depth=return_depth)
        if return_depth:
            xy, depth = xy
        uv = self._camera2image(xy)
        if return_depth:
            return uv, depth
        else:
            return uv

    def invproject(self, uv, directions=True, depth=1):
        """
        Project image coordinates to world coordinates or ray directions.

        Arguments:
            uv (array): Image coordinates (n, 2)
            directions (bool): Whether to return world ray directions relative
                to the camera position (True) or absolute coordinates by adding
                on the camera position (False)
            depth: Distance of rays along the camera's optical axis, as either a
                scalar or a vector (n, )

        Returns:
            array: World coordinates or ray directions (n, 3)
        """
        xy = self._image2camera(uv)
        xyz = self._camera2world(xy, directions=directions, depth=depth)
        return xyz

    def infront(self, xyz, directions=False):
        """
        Test whether world coordinates are in front of the camera.

        Arguments:
            xyz (array): World coordinates (n, 3)
            directions (bool): Whether `xyz` are ray directions (True)
                or absolute coordinates (False)
        """
        if directions:
            dxyz = xyz
        else:
            dxyz = xyz - self.xyz
        z = np.dot(dxyz, self.R.T[:, 2])
        return z > 0

    def inframe(self, uv):
        """
        Test whether image coordinates are in or on the image frame.

        Arguments:
            uv (array) Image coordinates (n, 2)
        """
        return np.all((uv >= 0) & (uv <= self.imgsz), axis=1)

    def inview(self, xyz, directions=False):
        """
        Test whether world coordinates are within view.

        Arguments:
            xyz (array): World coordinates (n, 3)
            directions (bool): Whether `xyz` are ray directions (True)
                or absolute coordinates (False)
        """
        uv = self.project(xyz, directions=directions)
        return self.inframe(uv)

    def grid(self, step, snap=None, mode='vectors'):
        """
        Return grid of image coordinates.

        Arguments:
            step: Grid spacing for all (float) or each (iterable) dimension
            snap (iterable): Point (x, y) to align grid to.
                If `None`, (0, 0) is used.
            mode (str): Return format

                - 'vectors': x (nx, ) and y (ny, ) coordinates
                - 'grids': x (ny, nx) and y (ny, nx) coordinates
                - 'points': x, y coordinates (ny * nx, 2)
        """
        box = (0, 0, self.imgsz[0], self.imgsz[1])
        vectors = helpers.box_to_grid(box, step=step, snap=snap, mode='vectors')
        if mode == 'vectors':
            return vectors
        grid = np.meshgrid(*vectors)
        if mode == 'grids':
            return grid
        if mode == 'points':
            return helpers.grid_to_points(grid)

    def edges(self, step=(1, 1)):
        """
        Return coordinates of image edges.

        Vertices are ordered clockwise from the origin (0, 0).

        Arguments:
            step (tuple): Pixel spacing of the vertices in x and y
        """
        if np.isscalar(step):
            step = (step, step)
        nu = (self.imgsz[0] / step[0] if step[0] else 1) + 1
        nv = (self.imgsz[1] / step[1] if step[1] else 1) + 1
        u = np.linspace(0, self.imgsz[0], int(nu))
        v = np.linspace(0, self.imgsz[1], int(nv))
        return np.vstack((
            np.column_stack((u, np.repeat(0, len(u)))),
            np.column_stack((np.repeat(u[-1], len(v) - 2), v[1:-1])),
            np.column_stack((u[::-1], np.repeat(v[-1], len(u)))),
            np.column_stack((np.repeat(0, len(v) - 2), v[::-1][1:-1]))
        ))

    def viewbox(self, depth, step=(1, 1)):
        """
        Return bounding box of the camera viewshed.

        The camera viewshed is constructed by projecting out edge pixels
        to a fixed depth.

        Arguments:
            depth (float): Distance of point projections
            step (tuple): Spacing of the projected pixels in x and y
        """
        uv = self.edges(step=step)
        dxyz = self.invproject(uv, depth=depth, directions=False)
        vertices = np.vstack((self.xyz, dxyz))
        return helpers.bounding_box(vertices)

    def viewpoly(self, depth, step=1, plane=None):
        """
        Return bounding polygon of the camera viewshed.

        The polygon is constructed by projecting out the pixel row passing
        through the principal point, then projecting the result onto a plane.

        Arguments:
            depth (float): Distance of point projections
            step (float): Spacing of the projected pixels
            plane (iterable): Plane (a, b, c, d), where ax + by + cz + d = 0.
                If `None`, no planar projection is performed.
        """
        n = int(self.imgsz[0] / step) + 1
        uv = np.column_stack((
            np.linspace(0, self.imgsz[0], n),
            np.repeat(self.imgsz[1] / 2 + self.c[1], n)))
        xyz = self.invproject(uv, directions=False, depth=depth)
        vertices = np.row_stack((self.xyz, xyz, self.xyz))
        if plane is None:
            return vertices
        else:
            return helpers.project_points_plane(points=vertices, plane=plane)

    def rasterize(self, uv, values, fun=np.mean):
        """
        Convert points to a raster image.

        Arguments:
            uv (array): Image point coordinates (Nx2)
            values (array): Point values
            fun (function): Aggregate function to apply to values of overlapping points
        """
        is_in = self.inframe(uv)
        shape = (int(self.imgsz[1]), int(self.imgsz[0]))
        return helpers.rasterize_points(uv[is_in, 1].astype(int), uv[is_in, 0].astype(int),
            values[is_in], shape, fun=fun)

    def spherical_to_xyz(self, angles):
        """
        Convert relative world spherical coordinates to euclidean.

        Arguments:
            angles (array): Spherical coordinates [azimuth, altitude(, distance)]

                - azimuth: degrees clockwise from north
                - altitude: degrees above horizon
                - distance: distance from camera

        Returns:
            array: World coordinates, either absolute (if distances were provided)
                or relative (if distances were not)
        """
        # https://en.wikipedia.org/wiki/Spherical_coordinate_system
        azimuth_iso = (np.pi / 2 - angles[:, 0] * np.pi / 180) % (2 * np.pi)
        altitude_iso = (np.pi / 2 - angles[:, 1] * np.pi / 180) % (2 * np.pi)
        xyz = np.column_stack((
            np.sin(altitude_iso) * np.cos(azimuth_iso),
            np.sin(altitude_iso) * np.sin(azimuth_iso),
            np.cos(altitude_iso)))
        directions = angles.shape[1] < 3
        if not directions:
            xyz *= angles[:, 2:3]
            xyz += self.xyz
        return xyz

    def xyz_to_spherical(self, xyz, directions=False):
        if not directions:
            xyz = xyz - self.cam.xyz
        r = np.sqrt(np.sum(xyz ** 2, axis=1))
        azimuth_iso = np.arctan2(xyz[:, 1], xyz[:, 0])
        altitude_iso = np.arccos(xyz[:, 2] / r)
        angles = np.column_stack((
            (90 - (azimuth_iso * 180 / np.pi)) % 360,
            90 - (altitude_iso * 180 / np.pi)))
        if not directions:
            angles = np.column_stack((angles, r))
        return angles

    def project_dem(self, dem, values=None, mask=None, tile_size=(256, 256),
        tile_overlap=(1, 1), scale=1, scale_limits=(1, 1), aggregate=np.mean,
        parallel=False, correction=False, return_depth=False):
        """
        Return an image simulated from a digital elevation model.

        If `parallel` is True and inputs are large, ensure that `dem`, `values`,
        and `mask` are in shared memory (see `sharedmem.copy()`).

        Arguments:
            dem (`Raster`): `Raster` object containing elevations.
            values (array): Values to use in building the image.
                Must have the same 2-dimensional shape as `dem.Z` but can have
                multiple layers stacked along the 3rd dimension.
                Cannot be `None` unless `return_depth` is True.
            mask (array): Boolean mask of cells of `dem` to include.
                Must have the same shape as `dem.Z`.
                If `None`, only NaN cells in `dem.Z` are skipped.
            tile_size (iterable): Target size of `dem` tiles (see `Grid.tile_indices()`)
            tile_overlap (iterable): Overlap between `dem` tiles (see `Grid.tile_indices()`)
            scale (float): Target `dem` cells per image pixel.
                Each tile is rescaled based on the average distance from the camera.
            scale_limits (iterable): Min and max values of `scale`
            aggregate: Passed as `func` to `pandas.DataFrame.aggregate()`
                to aggregate values projected onto the same image pixel.
                Each layer of `values`, and depth if `return_depth` is True,
                are named by their integer position in the stack (e.g. 0, 1, ...).
            parallel: Number of parallel processes (int),
                or whether to work in parallel (bool). If `True`,
                defaults to `os.cpu_count()`.
            correction: Whether or how to apply elevation corrections (see `helpers.elevation_corrections()`)
            return_depth: Whether to return a depth map - the distance of the
                `dem` surface measured along the camera's optical axis

        Returns:
            array: Array with 2-dimensional shape (`self.imgsz[1]`, `self.imgsz[0]`)
                and 3rd dimension corresponding to each layer in `values`.
                If `return_depth` is True, it is appended as an additional layer.
        """
        assert values is None or values.shape[0:2] == dem.shape
        assert mask is None or mask.shape == dem.shape
        if mask is None:
            mask = ~np.isnan(dem.Z)
        parallel = helpers._parse_parallel(parallel)
        has_values = values is not None
        if not has_values and not return_depth:
            raise ValueError('values cannot be missing if return_depth is False')
        if has_values:
            values = np.atleast_3d(values)
        # Generate DEM block indices
        tile_indices = dem.tile_indices(size=tile_size, overlap=tile_overlap)
        ntiles = len(tile_indices)
        # Initialize array
        # HACK: Use dummy DataFrame to predict output size of aggregate
        nbands_in = (values.shape[2] if has_values else 0) + return_depth
        df = pandas.DataFrame(
            data=np.zeros((2, nbands_in + 2)),
            columns=['row', 'col'] + list(range(nbands_in)))
        nbands_out = df.groupby(['row', 'col']).aggregate(aggregate).shape[1]
        I = np.full(self.shape + (nbands_out, ), np.nan)
        # Define parallel process
        bar = helpers._progress_bar(max=ntiles)
        def process(ij):
            tile_mask = mask[ij]
            if not np.count_nonzero(tile_mask):
                # No cells selected
                return None
            tile = dem[ij]
            if has_values:
                tile_values = values[ij]
            # Scale tile based on distance from camera
            mean_xyz = tile.xlim.mean(), tile.ylim.mean(), np.nanmean(tile.Z[tile_mask])
            if np.isnan(mean_xyz[2]):
                # No cells with elevations
                return None
            _, mean_depth = self._world2camera(np.atleast_2d(mean_xyz),
                return_depth=True, correction=correction)
            tile_scale = scale * np.abs(tile.d).mean() / (mean_depth / self.f.mean())
            tile_scale = min(max(tile_scale, min(scale_limits)), max(scale_limits))
            if tile_scale != 1:
                tile.resize(tile_scale)
                tile_mask = scipy.ndimage.zoom(tile_mask, zoom=float(tile_scale), order=0)
                tile_values = np.dstack(
                    scipy.ndimage.zoom(tile_values[:, :, i], zoom=float(tile_scale), order=1)
                    for i in range(tile_values.shape[2]))
            # Project tile
            xyz = helpers.grid_to_points((tile.X[tile_mask], tile.Y[tile_mask], tile.Z[tile_mask]))
            if return_depth:
                xy, depth = self._world2camera(xyz, correction=correction, return_depth=True)
                uv = self._camera2image(xy)
            else:
                uv = self.project(xyz, correction=correction)
            is_in = self.inframe(uv)
            if not np.count_nonzero(is_in):
                # No cells in image
                return None
            rc = uv[is_in, ::-1].astype(int)
            # Compile values
            if has_values:
                tile_values = tile_values[tile_mask][is_in]
            if return_depth:
                if has_values:
                    tile_values = np.column_stack((tile_values, depth[is_in, None]))
                else:
                    tile_values = depth[is_in, None]
            # Build DataFrame for fast groupby operation
            df = pandas.DataFrame(dict(row=rc[:, 0], col=rc[:, 1]))
            for i in range(tile_values.shape[1]):
                df.insert(df.shape[1], i, tile_values[:, i])
            # Aggregate values
            groups = df.groupby(('row', 'col')).aggregate(aggregate).reset_index()
            idx = (groups.row.as_matrix().astype(int),
                groups.col.as_matrix().astype(int))
            return idx, groups.iloc[:, 2:].as_matrix()
        def reduce(idx, values=None):
            bar.next()
            if idx is not None:
                I[idx] = values
        with config._MapReduce(np=parallel) as pool:
            pool.map(func=process, reduce=reduce, sequence=tile_indices)
        bar.finish()
        return I

    # ---- Methods (private) ----

    def _radial_distortion(self, r2):
        """
        Compute the radial distortion multipler `dr`.

        Arguments:
            r2 (array): Squared radius of camera coordinates (Nx1)
        """
        # dr = (1 + k1 * r^2 + k2 * r^4 + k3 * r^6) / (1 + k4 * r^2 + k5 * r^4 + k6 * r^6)
        dr = 1
        if self.k[0]:
            dr += self.k[0] * r2
        if self.k[1]:
            dr += self.k[1] * r2 * r2
        if self.k[2]:
            dr += self.k[2] * r2 * r2 * r2
        if any(self.k[3:6]):
            temp = 1
            if self.k[3]:
                temp += self.k[3] * r2
            if self.k[4]:
                temp += self.k[4] * r2 * r2
            if self.k[5]:
                temp += self.k[5] * r2 * r2 * r2
            dr /= temp
        return dr[:, None] # column

    def _tangential_distortion(self, xy, r2):
        """
        Compute tangential distortion additive `[dtx, dty]`.

        Arguments:
            xy (array): Camera coordinates (Nx2)
            r2 (array): Squared radius of `xy` (Nx1)
        """
        # dtx = 2xy * p1 + p2 * (r^2 + 2x^2)
        # dty = p1 * (r^2 + 2y^2) + 2xy * p2
        xty = xy[:, 0] * xy[:, 1]
        dtx = 2 * xty * self.p[0] + self.p[1] * (r2 + 2 * xy[:, 0]**2)
        dty = self.p[0] * (r2 + 2 * xy[:, 1]**2) + 2 * xty * self.p[1]
        return np.column_stack((dtx, dty))

    def _distort(self, xy):
        """
        Apply distortion to camera coordinates.

        Arguments:
            xy (array): Camera coordinates (Nx2)
        """
        # X' = dr * X + dt
        if not any(self.k) and not any(self.p):
            return xy
        else:
            dxy = xy.copy()
            r2 = np.sum(xy**2, axis=1)
            if any(self.k):
                dxy *= self._radial_distortion(r2)
            if any(self.p):
                dxy += self._tangential_distortion(xy, r2)
            return dxy

    def _undistort(self, xy, method='oulu', **params):
        """
        Remove distortion from camera coordinates.

        TODO: Quadtree 2-D bisection
        https://stackoverflow.com/questions/3513660/multivariate-bisection-method

        Arguments:
            xy (array): Camera coordinates (Nx2)
        """
        # X = (X' - dt) / dr
        if not any(self.k) and not any(self.p):
            return xy
        elif self.k[0] and not any(self.k[1:]) and not any(self.p):
            return self._undistort_k1(xy)
        elif method == 'lookup':
            return self._undistort_lookup(xy, **params)
        elif method == 'oulu':
            return self._undistort_oulu(xy, **params)
        elif method == 'regulafalsi':
            return self._undistort_regulafalsi(xy, **params)

    def _undistort_k1(self, xy):
        """
        Remove 1st order radial distortion.

        Uses the closed-form solution to the cubic equation when
        the only non-zero distortion coefficient is k1 (self.k[0]).

        Arguments:
            xy (array): Camera coordinates (Nx2)
        """
        # Cubic roots solution from Numerical Recipes in C 2nd Edition:
        # http://apps.nrbook.com/c/index.html (pages 183-185)
        # Solve for undistorted radius in polar coordinates
        # r^3 + r / k1 - r'/k1 = 0
        phi = np.arctan2(xy[:, 1], xy[:, 0])
        Q = - 1 / (3 * self.k[0])
        R = - xy[:, 0] / (2 * self.k[0] * np.cos(phi)) # r' = y / cos(phi)
        has_three_roots = R**2 < Q**3
        r = np.full(len(xy), np.nan)
        if np.any(has_three_roots):
          th = np.arccos(R[has_three_roots] * Q**-1.5)
          r[has_three_roots] = -2 * np.sqrt(Q) * np.cos((th - 2 * np.pi) / 3)
        has_one_root = ~has_three_roots
        if np.any(has_one_root):
          A = - np.sign(R[has_one_root]) * (np.abs(R[has_one_root]) + np.sqrt(R[has_one_root]**2 - Q**3))**(1.0 / 3)
          B = np.zeros(A.shape)
          not_zero = A != 0
          B[not_zero] = Q / A[not_zero]
          r[has_one_root] = A + B
        return np.column_stack((np.cos(phi), np.sin(phi))) * r[:, None]

    def _undistort_lookup(self, xy, density=1):
        """
        Remove distortion by table lookup.

        Creates a grid of test coordinates and applies distortion,
        then interpolates undistorted coordinates from the result
        with scipy.interpolate.LinearNDInterpolator().

        NOTE: Remains stable in extreme distortion, but slow
        for large lookup tables.

        Arguments:
            xy (array): Camera coordinates (Nx2)
            density (float): Grid points per pixel (approximate)
        """
        # Estimate undistorted camera coordinate bounds
        uv_edges = self.imgsz * np.array([
            [0, 0], [0.5, 0], [1, 0], [1, 0.5],
            [1, 1], [0.5, 1], [0, 1], [0, 0.5]
        ])
        xyu_edges = (uv_edges - (self.imgsz / 2 + self.c)) / self.f
        xyd_edges = self._distort(xyu_edges)
        # Build undistorted camera coordinates on regular grid
        ux = np.linspace(
            min(xyu_edges[:, 0].min(), xyd_edges[:, 0].min()),
            max(xyu_edges[:, 0].max(), xyd_edges[:, 0].max()),
            int(density * self.imgsz[0]))
        uy = np.linspace(
            min(xyu_edges[:, 1].min(), xyd_edges[:, 1].min()),
            max(xyu_edges[:, 1].max(), xyd_edges[:, 1].max()),
            int(density * self.imgsz[1]))
        UX, UY = np.meshgrid(ux, uy)
        uxy = np.column_stack((UX.flatten(), UY.flatten()))
        # Distort grid
        dxy = self._distort(uxy)
        # Interpolate distortion removal from gridded results
        # NOTE: Cannot use faster grid interpolation because dxy is not regular
        return scipy.interpolate.griddata(dxy, uxy, xy, method='linear')

    def _undistort_oulu(self, xy, iterations=20, tolerance=0):
        """
        Remove distortion by the iterative Oulu University method.

        See http://www.vision.caltech.edu/bouguetj/calib_doc/ (comp_distortion_oulu.m)

        NOTE: Converges very quickly in normal cases, but fails for extreme distortion.

        Arguments:
            xy (array): Camera coordinates (Nx2)
            iterations (int): Maximum number of iterations
            tolerance (float): Approximate pixel displacement in x and y below which
                to exit early, or `0` to disable early exit
        """
        uxy = xy # initial guess
        for n in range(iterations):
            r2 = np.sum(uxy**2, axis=1)
            if any(self.p) and not any(self.k):
                uxy = xy - self._tangential_distortion(uxy, r2)
            elif any(self.k) and not any(self.k):
                uxy = xy * (1 / self._radial_distortion(r2))
            else:
                uxy = (xy - self._tangential_distortion(uxy, r2)) * (1 / self._radial_distortion(r2))
            if tolerance > 0 and np.all((np.abs(self._distort(uxy) - xy)) < tolerance / self.f.mean()):
                break
        return uxy

    def _undistort_regulafalsi(self, xy, iterations=100, tolerance=0):
        """
        Remove distortion by iterative regula falsi (false position) method.

        See https://en.wikipedia.org/wiki/False_position_method

        NOTE: Almost always converges, but may require many iterations for extreme distortion.

        Arguments:
            xy (array): Camera coordinates (Nx2)
            iterations (int): Maximum number of iterations
            tolerance (float): Approximate pixel displacement in x and y (for all points)
                below which to exit early, or `0` to disable early exit (default)
        """
        # Start at center of image (distortion free)
        x1 = np.zeros(xy.shape, dtype=float)
        y1 = -xy
        # Then try halfway towards distorted coordinate
        # (more stable to approach solution from image center)
        x2 = xy / 2
        y2 = self._distort(x2) - xy
        uxy = np.full(xy.shape, np.nan)
        for n in range(iterations):
            dy = y2 - y1
            not_converged = np.all(dy != 0, axis=1)
            if tolerance > 0:
                not_converged &= np.any(np.abs(y2) > tolerance / self.f.mean())
            if n == 0:
                mask = np.ones(len(xy), dtype=bool)
            converged = np.zeros(mask.shape, dtype=bool)
            converged[mask] = ~not_converged
            uxy[converged] = x2[~not_converged]
            mask[mask] = not_converged
            x1 = x1[not_converged]
            y1 = y1[not_converged]
            x2 = x2[not_converged]
            y2 = y2[not_converged]
            if not np.any(not_converged):
                break
            x3 = (x1 * y2 - x2 * y1) / dy[not_converged]
            y3 = self._distort(x3) - xy[mask]
            x1 = x2
            y1 = y2
            x2 = x3
            y2 = y3
        uxy[mask] = x2
        return uxy

    def _reversible(self):
        """
        Test whether the camera model is reversible.

        Checks whether distortion produces a monotonically increasing result.
        If not, distorted coordinates are non-unique and cannot be reversed.

        TODO: Derive this explicitly from the distortion parameters.
        """
        xy_row = np.column_stack((
            np.linspace(-self.imgsz[0] / (2 * self.f[0]), self.imgsz[0] / (2 * self.f[0]), int(self.imgsz[0])),
            np.zeros(int(self.imgsz[0]))))
        dxy = self._distort(xy_row)
        continuous_row = np.all(dxy[1:, 0] >= dxy[:-1, 0])
        xy_col = np.column_stack((
            np.zeros(int(self.imgsz[1])),
            np.linspace(-self.imgsz[1] / (2 * self.f[1]), self.imgsz[1] / (2 * self.f[1]), int(self.imgsz[1]))))
        dxy = self._distort(xy_col)
        continuous_col = np.all(dxy[1:, 1] >= dxy[:-1, 1])
        return continuous_row and continuous_col

    def _world2camera(self, xyz, directions=False, correction=False,
        return_depth=False):
        """
        Project world coordinates to camera coordinates.

        Arguments:
            xyz (array): World coordinates (n, 3)
            directions (bool): Whether `xyz` are absolute coordinates (False)
                or ray directions (True)
            correction: Arguments to `helpers.elevation_corrections()` (dict),
                `True` for default arguments, or `None` or `False` to skip.
                Only applies if `directions` is `False`.
            return_depth (bool): Whether to return the distance of each point
                along the camera's optical axis
        """
        if directions:
            dxyz = xyz
        else:
            dxyz = xyz - self.xyz
            if correction is True:
                correction = dict()
            if isinstance(correction, dict):
                # Apply elevation correction
                dxyz[:, 2] += helpers.elevation_corrections(
                    squared_distances=np.sum(dxyz[:, 0:2]**2, axis=1),
                    **correction)
        # Convert coordinates to ray directions
        if config._UseMatMul:
            xyz_c = np.matmul(self.R, dxyz.T).T
        else:
            xyz_c = np.dot(dxyz, self.R.T)
        # Normalize by perspective division
        xy = xyz_c[:, 0:2] / xyz_c[:, 2:3]
        # Set points behind camera to NaN
        behind = xyz_c[:, 2] <= 0
        xy[behind, :] = np.nan
        if return_depth:
            return xy, xyz_c[:, 2]
        else:
            return xy

    def _camera2world(self, xy, directions=True, depth=1):
        """
        Project camera coordinates to world coordinates or ray directions.

        Arguments:
            xy (array): Camera coordinates (n, 2)
            directions (bool): Whether to return world ray directions relative
                to the camera position (True) or absolute coordinates by adding
                on the camera position (False)
            depth: Distance of rays along the camera's optical axis, as either a
                scalar or a vector (n, )
        """
        # Multiply 2-d coordinates
        if config._UseMatMul:
            xyz = np.matmul(self.R.T[:, 0:2], xy.T).T
        else:
            xyz = np.dot(xy, self.R[0:2, :])
        # Simulate z = 1 by adding 3rd column of rotation matrix
        xyz += self.R.T[:, 2]
        if depth != 1:
            xyz *= np.atleast_1d(depth).reshape(-1, 1)
        if not directions:
            xyz += self.xyz
        return xyz

    def _camera2image(self, xy):
        """
        Project camera to image coordinates.

        Arguments:
            xy (array): Camera coordinates (Nx2)
        """
        xy = self._distort(xy)
        uv = xy * self.f + (self.imgsz / 2 + self.c)
        return uv

    def _image2camera(self, uv):
        """
        Project image to camera coordinates.

        Arguments:
            uv (array): Image coordinates (Nx2)
        """
        xy = (uv - (self.imgsz * 0.5 + self.c)) * (1 / self.f)
        xy = self._undistort(xy)
        return xy

    def _image2camera_grid_ideal(self, uv):
        """
        Project image to camera coordinates.

        Faster version for an ideal camera and regularly gridded image coordinates.

        Arguments:
            uv (iterable): Vectors (u, v) of regularly gridded image coordinates
        """
        x = (uv[0] - (self.imgsz[0] * 0.5 + self.c[0])) * (1 / self.f[0])
        y = (uv[1] - (self.imgsz[1] * 0.5 + self.c[1])) * (1 / self.f[1])
        return helpers.grid_to_points(np.meshgrid(x, y))

class Exif(object):
    """
    Container and parser of image metadata.

    Provides access to the Exchangeable image file format (Exif) metadata tags
    embedded in an image file using :doc:`piexif <piexif:index>`.

    Arguments:
        path (str): Path to image.
            If `None`, :attr:`tags` will be empty.
        thumbnail (bool): Whether to retain the image thumbnail

    Attributes:
        tags (dict): Exif tags returned by :func:`piexif.load`.
            The tags are grouped by their Image File Directory (IFD):

                - 'Exif' (Exif SubIFD): Image generation
                - '0th' (IFD0): Main image
                - '1st' (IFD1): Thumbnail image
                - 'GPS' (GPS IFD): Position and trajectory
                - 'Interop' (Interoperability IFD): Compatibility

            The thumbnail image, if present, is stored as `bytes` in 'thumbnail'.

        size (numpy.ndarray): Image size in pixels (nx, ny).
            Parsed from 'PixelXDimension' and 'PixelYDimension'.
        datetime (datetime.datetime): Capture date and time.
            Parsed from 'DateTimeOriginal' and 'SubSecTimeOriginal'.
        exposure (float): Exposure time in seconds.
            Parsed from 'ExposureTime'.
        aperture (float): Aperture size as the f-number
            (https://wikipedia.org/wiki/F-number).
            Parsed from 'FNumber'.
        iso (float): Film speed following the ISO system
            (https://wikipedia.org/wiki/Film_speed#ISO).
            Parsed from 'ISOSpeedRatings'.
        fmm (float): Focal length in millimeters.
            Parsed from 'FocalLength'.
        make (str): Camera make.
            Parsed from 'Make'.
        model (str): Camera model.
            Parsed from 'Model'.
    """
    def __init__(self, path=None, thumbnail=False):
        if path:
            self.tags = piexif.load(path, key_is_name=True)
            if not thumbnail:
                self.tags.pop('thumbnail', None)
                self.tags.pop('1st', None)
            if self.size is None:
                im = osgeo.gdal.Open(path)
                self.tags['Exif']['PixelXDimension'] = im.RasterXSize
                self.tags['Exif']['PixelYDimension'] = im.RasterYSize
        else:
            self.tags = {}

    @property
    def size(self):
        width = self.parse_tag('PixelXDimension')
        height = self.parse_tag('PixelYDimension')
        if width and height:
            return np.array((width, height))
        else:
            return None

    @property
    def datetime(self):
        datetime_str = self.parse_tag('DateTimeOriginal')
        if not datetime_str:
            return None
        subsec_str = self.parse_tag('SubSecTimeOriginal')
        if subsec_str:
            return datetime.datetime.strptime(
                datetime_str + '.' + subsec_str, '%Y:%m:%d %H:%M:%S.%f')
        else:
            return datetime.datetime.strptime(
                datetime_str, '%Y:%m:%d %H:%M:%S')

    @property
    def exposure(self):
        return self.parse_tag('ExposureTime')

    @property
    def aperture(self):
        return self.parse_tag('FNumber')

    @property
    def iso(self):
        return self.parse_tag('ISOSpeedRatings')

    @property
    def fmm(self):
        return self.parse_tag('FocalLength')

    @property
    def make(self):
        return self.parse_tag('Make', group='0th')

    @property
    def model(self):
        return self.parse_tag('Model', group='0th')

    def parse_tag(self, tag, group='Exif'):
        """
        Return the parsed value of a tag.

        The following strategies are applied:

            - if `bytes`, decode to `str`
            - if `tuple` of length 2 (rational), convert to `float`
            - if `int`, convert to `float`

        Arguments:
            tag (str): Tag name
            group (str): Group name ('Exif', '0th', '1st', 'GPS', or 'Interop')

        Returns:
            object: Parsed tag value, or `None` if missing
        """
        try:
            value = self.tags[group][tag]
        except KeyError:
            return None
        if isinstance(value, bytes):
            return value.decode()
        elif isinstance(value, tuple) and len(value) == 2:
            return value[0] / value[1]
        elif isinstance(value, int):
            return float(value)
        else:
            return value

    def dump(self):
        """
        Return tags as bytes.

        The encoding is performed by :func:`piexif.dump`.
        The result can be embedded into an image file, for example using
        :func:`piexif.insert` or :meth:`PIL.Image.Image.save`.

        Returns:
            bytes: :attr:`tags` encoded as a byte string
        """
        # Copy tags before modifying inplace
        tags = copy.deepcopy(self.tags)
        # Replace key names with codes
        for group in self.tags.keys():
            if group == 'thumbnail':
                continue
            if group not in ('0th', '1st', 'Exif', 'GPS', 'Interop'):
                raise ValueError('Invalid group \'{0}\''.format(group))
            ifd_name = 'ImageIFD' if group in ('0th', '1st') else group + 'IFD'
            ifd = getattr(piexif, ifd_name)
            for tag in self.tags[group].keys():
                try:
                    code = getattr(ifd, tag)
                except AttributeError:
                    raise ValueError('Invalid tag \'{0}\' in group \'{1}\''
                        .format(tag, group))
                tags[group][code] = tags[group].pop(tag)
        # Encode to bytes
        return piexif.dump(tags)

class Image(object):
    """
    Photographic image and the settings that gave rise to the image.

    Arguments:
        path (str): Path to image
        cam (:class:`Camera`, dict, or str): Camera or arguments passed to
            Camera constructors:

                - if `dict`: Arguments passed to :class:`Camera()`.
                - if `str`: File path passed to :meth:`Camera.read`

            If 'imgsz' is missing, it is read from **exif**.
            If 'f', 'fmm', and 'sensorsz' are missing, 'fmm' is read from
            **exif** and 'sensorsz' is gotten from :meth:`Camera.get_sensor_size`
            with 'make' and 'model' from **exif**.
        exif (:class:`Exif`): Image metadata.
            If `None`, it is read from **path** with :class:`Exif()`.
        datetime (datetime.datetime): Capture date and time.
            If `None`, it is read from **exif.datetime**.
        anchor (bool):
        keypoints_path (str):

    Attributes:
        path (str): Path to image
        cam (:class:`Camera`): Camera
        exif (:class:`Exif`): Image metadata
        datetime (datetime.datetime): Capture date and time
        anchor (bool): Whether the camera parameters, especially view direction,
            are known absolutely. "Anchor" images are used as a reference for
            optimizing other images whose camera parameters are not known absolutely.
        keypoints_path (str): Path for caching image keypoints and their descriptors
            to a `pickle` file. Unless specified, defaults to `path` with a '.pkl' extension.
        keypoints: Cached keypoints
        I (numpy.ndarray): Cached image content
    """

    def __init__(self, path, cam=None, exif=None, datetime=None, anchor=False,
        keypoints_path=None):
        self.path = path
        if exif is None:
            exif = Exif(path=path)
        self.exif = exif
        self.anchor = anchor
        # NOTE: Namespace conflict with datetime (package)
        if datetime:
            self.datetime = datetime
        else:
            self.datetime = self.exif.datetime
        # TODO: Throw warning if `imgsz` has different aspect ratio than file size.
        if isinstance(cam, Camera):
            self.cam = cam
        else:
            if isinstance(cam, (bytes, str)):
                cam = helpers.read_json(cam)
            elif isinstance(cam, dict):
                cam = copy.deepcopy(cam)
            elif cam is None:
                cam = dict()
            if 'vector' not in cam:
                if 'imgsz' not in cam:
                    cam['imgsz'] = self.exif.size
                if 'f' not in cam:
                    if 'fmm' not in cam:
                        cam['fmm'] = self.exif.fmm
                    if 'sensorsz' not in cam and self.exif.make and self.exif.model:
                        cam['sensorsz'] = Camera.get_sensor_size(self.exif.make, self.exif.model)
            self.cam = Camera(**cam)
        self.I = None
        self.keypoints = None
        self.keypoints_path = keypoints_path

    def copy(self):
        """
        Return a copy.

        Copies camera, rereads exif from file, and does not copy cached image data (self.I).
        """
        return Image(path=self.path, cam=self.cam.copy())

    @require('osgeo')
    def read(self, box=None, cache=True):
        """
        Read image data from file.

        If the camera image size (self.cam.imgsz) differs from the original image size (self.exif.size),
        the image is resized to fit the camera image size.
        The result is cached (`self.I`) and reused only if it matches the camera image size, or,
        if not set, the original image size.
        To clear the cache, set `self.I` to `None`.

        Arguments:
            box (array-like): Crop extent in image coordinates (left, top, right, bottom)
                relative to `self.cam.imgsz`.
                If `cache=True`, the region is extracted from the cached image.
                If `cache=False`, the region is extracted directly from the file
                (faster than reading the entire image).
            cache (bool): Whether to save image in `self.I`
        """
        I = self.I
        if I is not None:
            size = np.flipud(I.shape[0:2])
        has_cam_size = all(~np.isnan(self.cam.imgsz))
        new_I = False
        if ((I is None) or
            (not has_cam_size and any(size != self.exif.size)) or
            (has_cam_size and any(size != self.cam.imgsz))):
            # Wrong size or not cached: Read image from file
            im = osgeo.gdal.Open(self.path)
            args = dict()
            original_size = (im.RasterXSize, im.RasterYSize)
            target_size = self.cam.imgsz.astype(int) if has_cam_size else original_size
            if any(target_size != original_size):
                # Read image into target-sized buffer
                args['buf_xsize'] = target_size[0]
                args['buf_ysize'] = target_size[1]
            if box is not None and not cache:
                # Resize box to image actual size
                scale = np.divide(original_size, target_size)
                # Read image subset
                args['xoff'] = int(round(box[0] * scale[0]))
                args['win_xsize'] = int(round((box[2] - box[0]) * scale[0]))
                args['yoff'] = int(round(box[1] * scale[1]))
                args['win_ysize'] = int(round((box[3] - box[1]) * scale[1]))
            I = np.stack([im.GetRasterBand(i + 1).ReadAsArray(**args)
                for i in range(im.RasterCount)], axis=2)
            if I.shape[2] == 1:
                I = I.squeeze(axis=2)
            if cache:
                # Caching: Cache result
                I = sharedmem.copy(I)
                self.I = I
            new_I = True
        if box is not None and (cache or not new_I):
            # Caching and cropping: Subset cached array
            I = I[box[1]:box[3], box[0]:box[2]]
        return I

    def write(self, path, I=None, **params):
        """
        Write image data to file.

        Arguments:
            path (str): File or directory path to write to.
                If the latter, the original basename is used.
                If the extension is unchanged and `I=None`, the original file is copied.
            I (array): Image data.
                If `None` (default), the original image data is read.
            **params: Additional arguments passed to `PIL.Image.save()`.
                See http://pillow.readthedocs.io/en/3.1.x/reference/Image.html#PIL.Image.Image.save
        """
        if os.path.isdir(path):
            # Use original basename
            path = os.path.join(path, os.path.basename(self.path))
        old_ext = os.path.splitext(self.path)[1].lower()
        ext = os.path.splitext(path)[1].lower()
        if ext == old_ext and I is None:
            # Copy original file
            shutil.copyfile(self.path, path)
        else:
            if I is None:
                im = PIL.Image.fromarray(self.read())
            else:
                im = PIL.Image.fromarray(I)
            # For JPEG file extensions, see https://stackoverflow.com/a/23424597/8161503
            if ext in ('.jpg', '.jpeg', '.jpe', '.jif', '.jfif', '.jfi'):
                exif = self.exif.copy()
                exif.set_tag('PixelXDimension', im.size[0])
                exif.set_tag('PixelYDimension', im.size[1])
                im.save(path, exif=exif.dump(), **params)
            else:
                warnings.warn('Writing EXIF to non-JPEG file is not supported')
                im.save(path, **params)

    def read_keypoints(self):
        """
        Return cached keypoints.

        Returns :attr:`keypoints` or reads them from :attr:`keypoints_path` with
        :func:`helpers.read_pickle`.
        Keypoints are expected to be in the form produced by
        :func:`optimize.detect_keypoints`.
        """
        if self.keypoints is None:
            if self.keypoints_path is None:
                warnings.warn('Keypoints path not specified')
                return None
            else:
                try:
                    self.keypoints = helpers.read_pickle(self.keypoints_path)
                except IOError:
                    warnings.warn('No keypoints found at keypoints path')
        return self.keypoints

    def write_keypoints(self):
        """
        Write keypoints to file.

        Writes :attr:`keypoints` to :attr:`keypoints_path` with
        :func:`helpers.write_pickle`.
        """
        if self.keypoints is not None and self.keypoints_path is not None:
            helpers.write_pickle(self.keypoints, path=self.keypoints_path)
        else:
            raise ValueError('No keypoints, or keypoints path not specified')

    def plot(self, origin='upper', extent=None, **params):
        """
        Plot image data.

        By default, the image is plotted with the upper-left corner of the upper-left pixel at (0,0).

        Arguments:
            origin (str): Place the [0, 0] index of the array in either the 'upper' left (default)
                or 'lower' left corner of the axes.
            extent (scalars): Location of the lower-left and upper-right corners (left, right, bottom, top).
                If `None` (default), the corners are positioned at (0, nx, ny, 0).
            **params: Additional arguments passed to `matplotlib.pyplot.imshow`.
        """
        I = self.read()
        if extent is None:
            extent=(0, I.shape[1], I.shape[0], 0)
        matplotlib.pyplot.imshow(I, origin=origin, extent=extent, **params)

    def set_plot_limits(self):
        matplotlib.pyplot.xlim(0, self.cam.imgsz[0])
        matplotlib.pyplot.ylim(self.cam.imgsz[1], 0)

    def project(self, cam, method='linear'):
        """
        Project image into another `Camera`.

        Arguments:
            cam (Camera): Target `Camera`
            method (str): Interpolation method, either 'linear' or 'nearest'
        """
        if not np.all(cam.xyz == self.cam.xyz):
            raise ValueError("Source and target cameras must have the same position ('xyz')")
        # Construct grid in target image
        u = np.linspace(0.5, cam.imgsz[0] - 0.5, int(cam.imgsz[0]))
        v = np.linspace(0.5, cam.imgsz[1] - 0.5, int(cam.imgsz[1]))
        U, V = np.meshgrid(u, v)
        uv = np.column_stack((U.flatten(), V.flatten()))
        # Project grid out target image
        dxyz = cam.invproject(uv)
        # Project target grid onto source image (flip for RegularGridInterpolator)
        pvu = np.fliplr(self.cam.project(dxyz, directions=True))
        # Construct grid in source image
        if cam.imgsz[0] == self.cam.imgsz[0]:
            pu = u
        else:
            pu = np.linspace(0.5, self.cam.imgsz[0] - 0.5, int(self.cam.imgsz[0]))
        if cam.imgsz[1] == self.cam.imgsz[1]:
            pv = v
        else:
            pv = np.linspace(0.5, self.cam.imgsz[1] - 0.5, int(self.cam.imgsz[1]))
        # Prepare source image
        I = self.read()
        if I.ndim < 3:
            I = np.expand_dims(I, axis=2)
        pI = np.full((int(cam.imgsz[1]), int(cam.imgsz[0]), I.shape[2]), np.nan, dtype=I.dtype)
        # Sample source image at target grid
        for i in range(pI.shape[2]):
            f = scipy.interpolate.RegularGridInterpolator((pv, pu), I[:, :, i], method=method, bounds_error=False)
            pI[:, :, i] = f(pvu).reshape(pI.shape[0:2])
        return pI
