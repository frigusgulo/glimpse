import numpy as np

class Tracker(object):
    """
    A `Tracker' describes a constant velocity model particle filter.
   
 
    Attributes:
        N (int): Number of particles (larger N gives better results at higher expense
        x0,y0 (float): Spatial coordinates to track
        dem (DEM): digital elevation model object corresponding to landscape of interest
        times (array): times at which to evaluate model and update with observation
        observers (list): List of Observer objects

        particles (Nx5 array): array storing the position and velocity of each particle
        weights (Nx1 array): likelihood of each particle for resampling and evaluating statistics
        means (1x5 array): mean of particle values
        covs (5x5 array): covariance of particle values
    """
    def __init__(self,N,x0,y0,dem,times,observers=[],dem_crop_distance=100):
        """
        Create a tracker instance

        Arguments:
            N (int): Number of particles (larger N gives better results at higher expense
            x0,y0 (float): Spatial coordinates to track
            dem (DEM): digital elevation model object corresponding to landscape of interest
            times (array): times at which to evaluate model and update with observation
            observers (list): List of Observer objects
            dem_crop_distance (float): distance from points at which dem should be cropped to increase evaluation speed

       
        """
        self.N = N
        self.x0 = x0
        self.y0 = y0
 
        # If dem_crop_distance is defined, return a more parsimonious DEM
        if dem_crop_distance is not None:
            x_bounds = np.array([x0-dem_crop_dist,x0+dem_crop_dist])
            y_bounds = np.array([y0-dem_crop_dist,y0+dem_crop_dist])
            self.dem = dem.crop(xlim=x_bounds,ylim=y_bounds)
        else:
            self.dem = dem
        self.times = times
        self.dt = np.hstack((0,np.diff(self.times)))
        self.observers = observers

        self.weights = np.ones(N)/N
        self.particles = np.zeros((N,5))

        # Keeps one from starting the tracker without initializing particles
        self.particles_are_initialized=False

    def initialize_particles(self,sigma_x0,sigma_y0,sigma_vx0,sigma_vy0,u0=0,v0=0):
        """
        Generate particles given initial distribution
     
        Arguments:
            sigma_x0,sigma_y0 (float): standard deviation of initial positions
            sigma_vx0,sigma_vy0 (float): standard deviation of initial velocity
            u0,v0 (float,optional): Initial velocity mean
        
        """
        self.particles[:,0] = self.x0 + sigma_x0*np.random.randn(self.N)
        self.particles[:,1] = self.y0 + sigma_y0*np.random.randn(self.N)
        self.particles[:,2] = u0 + sigma_vx0*np.random.randn(self.N)
        self.particles[:,3] = v0 + sigma_vy0*np.random.randn(self.N)
        self.particles[:,4] = self.dem.sample(self.particles[:,[0,1]])
        self.particles_are_initialized=True

    def _estimate(self):
        """
        Return the mean and covariance matrices of the current particle state
        """
        mean = np.average(self.particles,weights=self.weights,axis=0)
        cov = np.cov(self.particles.T,aweights=self.weights)
        return mean,cov

    def _predict(self,dt,wx,wy):
        """
        Apply one step of particle evolution according to stochastic DE

        Arguments:
            dt (float): time step
            wx,wy (float): standard deviation of random accelerations
        """
        ax = wx*np.random.randn(self.N)
        ay = wy*np.random.randn(self.N)
        particles[:,0] += dt*particles[:,2] + 0.5*ax*dt**2
        particles[:,1] += dt*particles[:,3] + 0.5*ay*dt**2
        particles[:,2] += dt*ax*np.random.randn(N)
        particles[:,3] += dt*ay*np.random.randn(N)
        particles[:,4] = self.dem.sample(particles[:,[0,1]])

    def _update(self,log_likelihoods):
        """
        Update the particle filter weights based on log likelihoods from observers
        Arguments:
            log_likelihoods (array): log likelihood for each particle (sum of log likelihoods from observers)
        """ 
        self.weights.fill(1.)
        self.weights *= np.exp(-sum(log_likelihoods))
        self.weights += 1e-300
        self.weights /= self.weights.sum()

    def _systematic_resample(self):
        """
        Systematic resampling of particles (kills unlikely particles, and reproduces likely ones)
        """
        # make N subdivisions, choose positions
        # with a consistent random offset
        positions = (np.arange(self.N) + np.random.random()) / self.N

        indexes = np.zeros(self.N, 'i')
        cumulative_sum = np.cumsum(self.weights)
        i, j = 0, 0
        while i < self.N:
            if positions[i] < cumulative_sum[j]:
                indexes[i] = j
                i += 1
            else:
                j += 1

        self.particles = self.particles[indexes]
        self.weights = self.weights[indexes]
        self.weights /= np.sum(self.weights)

    def track(self,wx,wy,do_plot=False):
        """
        Commence particle filtering
        
        Arguments:
            wx,wy: random acceleration std
            do_plot (optional): Plot or don't
        """
        try:
            assert(self.particles_are_initialized)
        except AssertionError:
            print("Particles are not initialized!")
            return 

        mean,cov = self._estimate()
        self.means = [mean]
        self.cov = [cov]
        if do_plot:
            print "Plotting not yet implemented"
            return
            #self.initialize_plot()
 
        for t,dt in zip(self.times[1:],self.dt[1:]):
            self._predict(dt,wx,wy)
            pmean,pcov = self._estimate()
            log_likelihoods = [observer.compute_likelihood(pmean,self.particles,t) for observer in self.observers]
            self._update(log_likelihoods)
            self._systematic_resample()
            mean,cov = self._estimate()
            self.means.append(new_mean)
            self.covs.append(new_cov)
            #if do_plot:
            #    self.update_plot()

              