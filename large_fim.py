# ====================================================================================== #
# Classes for calculating FIM on Ising and Potts models for large systems where sampling
# is necessary.
# Author : Eddie Lee, edlee@alumni.princeton.edu
# ====================================================================================== #
import numpy as np
from numba import njit
from coniii.utils import *
from .utils import *
from warnings import warn
from itertools import combinations, product
from coniii.enumerate import fast_logsumexp, mp_fast_logsumexp
from multiprocess import Pool, cpu_count
import mpmath as mp
from scipy.sparse import coo_matrix
from .models import LargeIsing, LargePotts3
np.seterr(divide='ignore')


class Magnetization():
    """Perturbation of local magnetizations one at a time. By default, perturbation is
    towards +1 and not -1.
    """
    def __init__(self, n,
                 h=None,
                 J=None,
                 eps=1e-7,
                 precompute=True,
                 n_cpus=None,
                 n_samples=10_000_000):
        """
        Parameters
        ----------
        n : int
        h : ndarray, None
        J : ndarray, None
        eps : float, 1e-7
        precompute : bool, True
        n_cpus : int, None
        n_samples : int, 10_000_000
            Number of samples for Metropolis sampling.
        """

        assert n>1 and 0<eps<.1
        self.n = n
        self.kStates = 2
        self.eps = eps
        self.hJ = np.concatenate((h,J))
        self.n_cpus = n_cpus

        self.ising = LargeIsing((h,J), n_samples)
        self.sisj = np.concatenate(self.ising.corr[:2])
        self.p = self.ising.p
        self.allStates = self.ising.states.astype(np.int8)
        _, self.coarseInvix = np.unique(np.abs(self.allStates.sum(1)), return_inverse=True)
        self.coarseUix = np.unique(self.coarseInvix)
        
        # cache triplet and quartet products
        self._triplets_and_quartets() 
    
        if precompute:
            self.dJ = self.compute_dJ()
        else:
            self.dJ = np.zeros((self.n,self.n+(self.n-1)*self.n//2))

        self._custom_end_init()

    def _custom_end_init(self):
        """Placeholder that can be replaced in children classes."""
        return
    
    def _triplets_and_quartets(self):
        self.triplets, self.quartets = jit_triplets_and_quartets(self.n, self.allStates.astype(np.int8)) 

    def compute_dJ(self, p=None, sisj=None):
        # precompute linear change to parameters for small perturbation
        dJ = np.zeros((self.n,self.n+(self.n-1)*self.n//2))
        for i in range(self.n):
            dJ[i], errflag = self.solve_linearized_perturbation(i, p=p, sisj=sisj)
        return dJ

    def observables_after_perturbation(self, i,
                                       eps=None):
        """Perturb all specified spin by forcing it point upwards with probability eps/2.
        Perturb the corresponding mean and the correlations with other spins j.
        
        Parameters
        ----------
        i : int
            Spin to perturb.
        eps : float, None

        Returns
        -------
        ndarray
            Observables <si> and <sisj> after perturbation.
        bool, True
            If True, made the specified spin point up +1. If False, made it point down -1.
        """
        
        if not hasattr(i,'__len__'):
            i = (i,)
        if not hasattr(eps,'__len__'):
            eps = eps or self.eps
            eps = [eps]*len(i)
        n = self.n
        si = self.sisj[:n]
        sisj = self.sisj[n:]
       
        siNew = si.copy()
        sisjNew = sisj.copy()
        
        # observables after perturbations
        for i_,eps_ in zip(i,eps):
            jit_observables_after_perturbation_plus_field(n, siNew, sisjNew, i_, eps_)
        perturb_up = True

        return np.concatenate((siNew, sisjNew)), perturb_up
   
    def _observables_after_perturbation_plus_field(self, n, si, sisj, i, eps):
        """        
        Parameters
        ----------
        n : int
        si : ndarray
        sisj : ndarray
        i : int
        eps : float
        """

        # observables after perturbations
        si[i]  = (1-eps)*si[i] + eps

        for j in delete(list(range(n)),i):
            if i<j:
                ijix = unravel_index((i,j),n)
            else:
                ijix = unravel_index((j,i),n)
            sisj[ijix] = (1-eps)*sisj[ijix] + eps*si[j]

    def _solve_linearized_perturbation_tester(self, iStar, eps=None):
        """Consider a perturbation to a single spin.
        
        Parameters
        ----------
        iStar : int
        eps : float, None
        perturb_up : bool, False

        Returns
        -------
        ndarray
            Linear change in maxent parameters for given iStar.
        """
        
        from coniii.solvers import Enumerate

        n = self.n
        p = self.p
        if eps is None:
            eps = self.eps
        C, perturb_up = self.observables_after_perturbation(iStar, eps=eps)

        solver = Enumerate(np.ones((1,n)))
        if perturb_up:
            return (solver.solve(constraints=C) - self.hJ) / eps

        # account for sign of perturbation on fields
        dJ = -(solver.solve(constraints=C) - self.hJ) / eps
        return dJ

    def solve_linearized_perturbation(self, *args, **kwargs):
        """Wrapper for automating search for best eps value for given perturbation.
        """
        
        # settings
        epsChangeFactor = 10
        
        # check whether error increases or decreases with eps
        eps0 = kwargs.get('eps', self.eps)
        kwargs['check_stability'] = True
        kwargs['full_output'] = True
        
        dJ, errflag, (A,C), relerr = self._solve_linearized_perturbation(*args, **kwargs)

        kwargs['eps'] = eps0*epsChangeFactor
        dJUp, errflagUp, _, relerrUp = self._solve_linearized_perturbation(*args, **kwargs)

        kwargs['eps'] = eps0/epsChangeFactor
        dJDown, errflagDown, _, relerrDown = self._solve_linearized_perturbation(*args, **kwargs)
        
        # if changing eps doesn't help, just return estimate at current eps
        if relerr.max()<relerrUp.max() and relerr.max()<relerrDown.max():
            return dJ, errflag
        
        # if error decreases more sharpy going down
        if relerrDown.max()<=relerrUp.max():
            epsChangeFactor = 1/epsChangeFactor
            prevdJ, errflag, prevRelErr = dJDown, errflagDown, relerrDown
        # if error decreases more sharpy going up, no need to change eps
        else:
            prevdJ, errflag, prevRelErr = dJUp, errflagUp, relerrUp
        
        # decrease/increase eps til error starts increasing
        converged = False
        while (not converged) and errflag:
            kwargs['eps'] *= epsChangeFactor
            dJ, errflag, (A,C), relerr = self._solve_linearized_perturbation(*args, **kwargs)
            if errflag and relerr.max()<prevRelErr.max():
                prevdJ = dJ
                prevRelErr = relerr
            else:
                converged = True
        
        return dJ, errflag

    def _solve_linearized_perturbation(self, iStar,
                                      p=None,
                                      sisj=None,
                                      full_output=False,
                                      eps=None,
                                      check_stability=True,
                                      method='inverse'):
        """Consider a perturbation to a single spin.
        
        Parameters
        ----------
        iStar : int
        p : ndarray, None
        sisj : ndarray, None
        full_output : bool, False
        eps : float, None
        check_stability : bool, False
        method : str, 'inverse'
            Can be 'inverse' or 'lstsq'

        Returns
        -------
        ndarray
            dJ
        int
            Error flag. Returns 0 by default. 1 means badly conditioned matrix A.
        tuple (optional)
            (A,C)
        """
        
        perturb_up = False
        eps = eps or self.eps
        n = self.n
        if p is None:
            p = self.p
        if sisj is None:
            si = self.sisj[:n]
            sisj = self.sisj[n:]
        else:
            si = sisj[:n]
            sisj = sisj[n:]
        A = np.zeros((n+n*(n-1)//2, n+n*(n-1)//2), dtype=si.dtype)
        C, perturb_up = self.observables_after_perturbation(iStar, eps=eps)
        
        # mean constraints
        for i in range(n):
            for k in range(n):
                if i==k:
                    A[i,i] = 1 - C[i]*si[i]
                else:
                    if i<k:
                        ikix = unravel_index((i,k),n)
                    else:
                        ikix = unravel_index((k,i),n)
                    A[i,k] = sisj[ikix] - C[i]*si[k]

            for klcount,(k,l) in enumerate(combinations(range(n),2)):
                A[i,n+klcount] = self.triplets[(i,k,l)].dot(p) - C[i]*sisj[klcount]
        
        # pair constraints
        for ijcount,(i,j) in enumerate(combinations(range(n),2)):
            for k in range(n):
                A[n+ijcount,k] = self.triplets[(i,j,k)].dot(p) - C[n+ijcount]*si[k]
            for klcount,(k,l) in enumerate(combinations(range(n),2)):
                A[n+ijcount,n+klcount] = self.quartets[(i,j,k,l)].dot(p) - C[n+ijcount]*sisj[klcount]
    
        C -= self.sisj
        if method=='inverse':
            # factor out linear dependence on eps
            try:
                dJ = np.linalg.solve(A,C)/eps
            except np.linalg.LinAlgError:
                dJ = np.zeros(C.size)+np.nan
        else:
            dJ = np.linalg.lstsq(A, C, rcond=None)[0]/eps
        # Since default is to perturb down
        if not perturb_up:
            dJ *= -1

        if check_stability:
            # double epsilon and make sure solution does not change by a large amount
            dJtwiceEps, errflag = self._solve_linearized_perturbation(iStar,
                                                                      eps=eps/2,
                                                                      check_stability=False,
                                                                      p=p,
                                                                      sisj=np.concatenate((si,sisj)))
            # print if relative change is more than .1% for any entry
            relerr = np.log10(np.abs(dJ-dJtwiceEps))-np.log10(np.abs(dJ))
            if (relerr>-3).any():
                print("Unstable solution. Recommend shrinking eps. %E"%(10**relerr.max()))
        else:
            relerr = None
                   
        if (np.linalg.cond(A)>1e15):
            warn("A is badly conditioned.")
            errflag = 1
        else:
            errflag = 0
        if full_output:
            return dJ, errflag, (A, C), relerr
        return dJ, errflag
    
    def dkl_curvature(self, *args, **kwargs):
        """Wrapper for _dkl_curvature() to find best finite diff step size."""

        if not 'epsdJ' in kwargs.keys():
            kwargs['epsdJ'] = 1e-4
        if not 'check_stability' in kwargs.keys():
            kwargs['check_stability'] = True
        if 'full_output' in kwargs.keys():
            full_output = kwargs['full_output']
        else:
            full_output = False
        kwargs['full_output'] = True
        epsDecreaseFactor = 10
        
        converged = False
        prevHess, errflag, prevNormerr = self._dkl_curvature(*args, **kwargs)
        kwargs['epsdJ'] /= epsDecreaseFactor
        while (not converged) and errflag:
            hess, errflag, normerr = self._dkl_curvature(*args, **kwargs)
            # end loop if error starts increasing again
            if errflag and normerr<prevNormerr:
                prevHess = hess
                prevNormerr = normerr
                kwargs['epsdJ'] /= epsDecreaseFactor
            else:
                converged = True
        if not converged and not errflag:
            normerr = None
        hess = prevHess
        
        if full_output:
            return hess, errflag, normerr
        return hess

    def _dkl_curvature(self,
                      hJ=None,
                      dJ=None,
                      epsdJ=1e-4,
                      n_cpus=None,
                      check_stability=False,
                      rtol=1e-3,
                      zero_out_small_p=True,
                      p_threshold=1e-15,
                      full_output=False):
        """Calculate the hessian of the KL divergence (Fisher information metric) w.r.t.
        the theta_{ij} parameters replacing the spin i by sampling from j.

        Use single step finite difference method to estimate Hessian.
        
        Parameters
        ----------
        hJ : ndarray, None
            Ising model parameters.
        dJ : ndarray, None
            Linear perturbations in parameter space corresponding to Hessian at given hJ.
            These can be calculuated using self.solve_linearized_perturbation().
        epsdJ : float, 1e-4
            Step size for taking linear perturbation wrt parameters.
        n_cpus : int, None
        check_stability : bool, False
        rtol : float, 1e-3
            Relative tolerance for each entry in Hessian when checking stability.
        zero_out_small_p : bool, True
            If True, set all small values below p_threshold to 0.
        p_threshold : float, 1e-15
        full_output : bool, False
            
        Returns
        -------
        ndarray
            Hessian.
        int (optional)
            Error flag. 1 indicates rtol was exceeded. None indicates that no check was
            done.
        float (optional)
            Norm difference between hessian with step size eps and eps/2.
        """
        
        n = self.n
        if hJ is None:
            hJ = self.hJ
            p = self.p
        else:
            p = self.ising.p(hJ)
        log2p = np.log2(p)
        if dJ is None:
            dJ = self.dJ

        if zero_out_small_p:
            log2p[p<p_threshold] = -np.inf
            p = p.copy()
            p[p<p_threshold] = 0.
        
        # diagonal entries
        def diag(i, hJ=hJ, ising=self.ising, dJ=dJ, p=p):
            newhJ = hJ.copy()
            newhJ += dJ[i]*epsdJ
            modp = ising.p(newhJ)
            return np.nansum(2*(log2p-np.log2(modp))*p) / epsdJ**2
            
        # Compute off-diagonal entries. These don't account for the subtraction of the
        # diagonal elements which are removed later To see this, expand D(theta_i+del,
        # theta_j+del) to second order.
        def off_diag(args, hJ=hJ, ising=self.ising, dJ=dJ, p=p):
            i, j = args
            newhJ = hJ.copy()
            newhJ += (dJ[i]+dJ[j])*epsdJ
            modp = ising.p(newhJ)
            return np.nansum((log2p-np.log2(modp))*p) / epsdJ**2
        
        hess = np.zeros((len(dJ),len(dJ)))
        if (not n_cpus is None) and n_cpus<=1:
            for i in range(len(dJ)):
                hess[i,i] = diag(i)
            for i,j in combinations(range(len(dJ)),2):
                hess[i,j] = hess[j,i] = off_diag((i,j))
        else:
            hess[np.eye(len(dJ))==1] = self.pool.map(diag, range(len(dJ)))
            hess[np.triu_indices_from(hess,k=1)] = self.pool.map(off_diag, combinations(range(len(dJ)),2))
            # subtract off linear terms to get Hessian (and not just cross derivative)
            hess[np.triu_indices_from(hess,k=1)] -= np.array([hess[i,i]/2+hess[j,j]/2
                                                            for i,j in combinations(range(len(dJ)),2)])
            # fill in lower triangle
            hess += hess.T
            hess[np.eye(len(dJ))==1] /= 2

        if check_stability:
            hess2 = self.dkl_curvature(epsdJ=epsdJ/2, check_stability=False, hJ=hJ, dJ=dJ)
            err = hess2 - hess
            if (np.abs(err/hess) > rtol).any():
                normerr = np.linalg.norm(err)
                errflag = 1
                msg = ("Finite difference estimate has not converged with rtol=%f. "+
                       "May want to shrink epsdJ. Norm error %f.")
                print(msg%(rtol,normerr))
            else:
                errflag = 0
                normerr = None
        else:
            errflag = None
            normerr = None

        if not full_output:
            return hess
        return hess, errflag, normerr
    
    @staticmethod
    def p2pk(p, uix, invix):
        """Convert the full probability distribution to the probability of having k votes
        in the majority.

        Parameters
        ----------
        p : ndarray
        uix : ndarray
        invix : ndarray

        Returns
        -------
        ndarray
            p(k)
        """
         
        pk = np.zeros(len(uix))
        for i in range(len(uix)):
            pk[i] = p[invix==i].sum()

        return pk

    @staticmethod
    def logp2pk(E, uix, invix):
        """Convert the full probability distribution to the probability of having k votes
        in the majority.

        Parameters
        ----------
        E : ndarray
            Energies of each configuration.
        uix : ndarray
        invix : ndarray

        Returns
        -------
        ndarray
            The unnormalized log probability: log p(k) + logZ.
        """
         
        logsumEk = np.zeros(len(uix))
        for i in range(len(uix)):
            logsumEk[i] = fast_logsumexp(-E[invix==i])[0]
        return logsumEk

    @staticmethod
    def p2pk_high_prec(p, uix, invix):
        """Convert the full probability distribution to the probability of having k votes
        in the majority. Assuming that n is odd.

        High precision version (p is an array of mp.mpf types).

        Parameters
        ----------
        p : ndarray

        Returns
        -------
        ndarray
            p(k)
        """
        
        pk = np.zeros(len(uix), dtype=object)
        for i in range(len(uix)):
            pk[i] = p[invix==i].sum()

        return pk

    @staticmethod
    def logp2pk_high_prec(E, uix, invix):
        """Convert the full probability distribution to the probability of having k votes
        in the majority.

        Parameters
        ----------
        E : ndarray
            Energies of each configuration.
        uix : ndarray
        invix : ndarray

        Returns
        -------
        ndarray
            The unnormalized log probability: log p(k) + logZ.
        """
         
        logsumEk = np.zeros(len(uix), dtype=object)
        for i in range(len(uix)):
            logsumEk[i] = mp_fast_logsumexp(-E[invix==i])[0]
        return logsumEk

    def maj_curvature(self, *args, **kwargs):
        """Wrapper for _maj_curvature() to find best finite diff step size."""

        import multiprocess as mp

        if not 'epsdJ' in kwargs.keys():
            kwargs['epsdJ'] = 1e-4
        if not 'check_stability' in kwargs.keys():
            kwargs['check_stability'] = True
        if 'full_output' in kwargs.keys():
            full_output = kwargs['full_output']
        else:
            full_output = False
        if 'high_prec' in kwargs.keys():
            high_prec = kwargs['high_prec']
            del kwargs['high_prec']
        else:
            high_prec = False
        kwargs['full_output'] = True
        epsDecreaseFactor = 10
        
        try:
            if self.n_cpus is None or self.n_cpus>1:
                n_cpus = self.n_cpus or mp.cpu_count()
                self.pool = mp.Pool(n_cpus,maxtasksperchild=1)

            # start loop for finding optimal eps for Hessian with num diff
            converged = False
            if high_prec:
                prevHess, errflag, preverr = self._maj_curvature_high_prec(*args, **kwargs)
            else:
                prevHess, errflag, preverr = self._maj_curvature(*args, **kwargs)
            kwargs['epsdJ'] /= epsDecreaseFactor
            while (not converged) and errflag:
                if high_prec:
                    hess, errflag, err = self._maj_curvature_high_prec(*args, **kwargs)
                else:
                    hess, errflag, err = self._maj_curvature(*args, **kwargs)
                # end loop if error starts increasing again
                if errflag and np.linalg.norm(err)<np.linalg.norm(preverr):
                    prevHess = hess
                    preverr = err
                    kwargs['epsdJ'] /= epsDecreaseFactor
                else:
                    converged = True
        finally:
            if self.n_cpus is None or self.n_cpus>1:
                self.pool.close()
                del self.pool

        hess = prevHess
        err = preverr
        
        if full_output:
            return hess, errflag, err
        return hess

    def _maj_curvature(self,
                       hJ=None,
                       dJ=None,
                       epsdJ=1e-7,
                       check_stability=False,
                       rtol=1e-3,
                       full_output=False,
                       calc_off_diag=True,
                       calc_diag=True,
                       iprint=True):
        """Calculate the hessian of the KL divergence (Fisher information metric) w.r.t.
        the theta_{ij} parameters replacing the spin i by sampling from j for the number
        of k votes in the majority.

        Use single step finite difference method to estimate Hessian.
        
        Parameters
        ----------
        hJ : ndarray, None
            Ising model parameters.
        dJ : ndarray, None
            Linear perturbations in parameter space corresponding to Hessian at given hJ.
            These can be calculuated using self.solve_linearized_perturbation().
        epsdJ : float, 1e-4
            Step size for taking linear perturbation wrt parameters.
        check_stability : bool, False
        rtol : float, 1e-3
            Relative tolerance for each entry in Hessian when checking stability.
        full_output : bool, False
        calc_off_diag : bool, True
        calc_diag : bool, True
        iprint : bool, True
            
        Returns
        -------
        ndarray
            Hessian.
        int (optional)
            Error flag. 1 indicates rtol was exceeded. None indicates that no check was
            done.
        float (optional)
            Norm difference between hessian with step size eps and eps/2.
        """

        n = self.n
        if hJ is None:
            hJ = self.hJ
        E = calc_e(self.allStates, hJ)
        logZ = fast_logsumexp(-E)[0]
        logsumEk = self.logp2pk(E, self.coarseUix, self.coarseInvix)
        p = np.exp(logsumEk - logZ)
        assert np.isclose(p.sum(),1), p.sum()
        if dJ is None:
            dJ = self.dJ
            assert self.dJ.shape[1]==(n+n*(n-1)//2)
        if iprint:
            print('Done with preamble.')

        # diagonal entries of hessian
        def diag(i, hJ=hJ, dJ=dJ, p=self.p, pk=p, logp2pk=self.logp2pk,
                 uix=self.coarseUix, invix=self.coarseInvix,
                 n=self.n, E=E, logZ=logZ, allStates=self.allStates):
            # round eps step to machine precision
            mxix = np.abs(dJ[i]).argmax()
            newhJ = hJ[mxix] + dJ[i][mxix]*epsdJ
            epsdJ_ = (newhJ-hJ[mxix]) / dJ[i][mxix]
            if np.isnan(epsdJ_): return 0.
            correction = calc_e(allStates, dJ[i]*epsdJ_)
            correction = np.array([correction[invix==ix].dot(p[invix==ix])/p[invix==ix].sum()
                                   for ix in range(uix.size)])
            num = ((correction.dot(pk) - correction)**2).dot(pk)
            dd = num / np.log(2) / epsdJ_**2
            if iprint and np.isnan(dd):
                print('nan for diag', i, epsdJ_)
            
            return dd

        # off-diagonal entries of hessian
        def off_diag(args, hJ=hJ, dJ=dJ, p=self.p, pk=p, logp2pk=self.logp2pk,
                     uix=self.coarseUix, invix=self.coarseInvix,
                     n=self.n, E=E, logZ=logZ, allStates=self.allStates):
            i, j = args
            
            # round eps step to machine precision
            mxix = np.abs(dJ[i]).argmax()
            newhJ = hJ[mxix] + dJ[i][mxix]*epsdJ
            epsdJi = (newhJ - hJ[mxix])/dJ[i][mxix]/2
            if np.isnan(epsdJi): return 0.
            correction = calc_e(allStates, dJ[i]*epsdJi)
            correctioni = np.array([correction[invix==ix].dot(p[invix==ix])/p[invix==ix].sum()
                                    for ix in range(uix.size)])

            # round eps step to machine precision
            mxix = np.abs(dJ[j]).argmax()
            newhJ = hJ[mxix] + dJ[j][mxix]*epsdJ
            epsdJj = (newhJ - hJ[mxix])/dJ[j][mxix]/2
            if np.isnan(epsdJj): return 0.
            correction = calc_e(allStates, dJ[j]*epsdJj)
            correctionj = np.array([correction[invix==ix].dot(p[invix==ix])/p[invix==ix].sum()
                                    for ix in range(uix.size)])

            num = ((correctioni.dot(pk) - correctioni)*(correctionj.dot(pk) - correctionj)).dot(pk)
            dd = num / np.log(2) / (epsdJi * epsdJj)
            if iprint and np.isnan(dd):
                print('nan for off diag', args, epsdJi, epsdJj)
            return dd
        
        hess = np.zeros((len(dJ),len(dJ)))
        if not 'pool' in self.__dict__.keys():
            warn("Not using multiprocess can lead to excessive memory usage.")
            if calc_diag:
                for i in range(len(dJ)):
                    hess[i,i] = diag(i)
                if iprint:
                    print("Done with diag.")
            if calc_off_diag:
                for i,j in combinations(range(len(dJ)),2):
                    hess[i,j] = off_diag((i,j))
                    if iprint:
                        print("Done with off diag (%d,%d)."%(i,j))
                if iprint:
                    print("Done with off diag.")
        else:
            if calc_diag:
                hess[np.eye(len(dJ))==1] = self.pool.map(diag, range(len(dJ)))
                if iprint:
                    print("Done with diag.")
            if calc_off_diag:
                hess[np.triu_indices_from(hess,k=1)] = self.pool.map(off_diag, combinations(range(len(dJ)),2))
                if iprint:
                    print("Done with off diag.")

        if calc_off_diag:
            # fill in lower triangle
            hess += hess.T
            hess[np.eye(len(dJ))==1] /= 2

        # check for precision problems
        assert ~np.isnan(hess).any(), hess
        assert ~np.isinf(hess).any(), hess

        if check_stability:
            hess2 = self._maj_curvature(epsdJ=epsdJ/2,
                                        check_stability=False,
                                        iprint=iprint,
                                        hJ=hJ,
                                        dJ=dJ,
                                        calc_diag=calc_diag,
                                        calc_off_diag=calc_off_diag)
            err = hess - hess2
            if (np.abs(err/hess) > rtol).any():
                errflag = 1
                if iprint:
                    msg = ("Finite difference estimate has not converged with rtol=%f. "+
                           "May want to shrink epsdJ. Norm error %f.")
                    print(msg%(rtol,np.linalg.norm(err)))
            else:
                errflag = 0
                if iprint:
                    msg = "Finite difference estimate converged with rtol=%f."
                    print(msg%rtol)
        else:
            errflag = None
            err = None

        if not full_output:
            return hess
        return hess, errflag, err

    def _maj_curvature_high_prec(self,
                                 hJ=None,
                                 dJ=None,
                                 epsdJ=1e-4,
                                 n_cpus=None,
                                 check_stability=False,
                                 rtol=1e-3,
                                 full_output=False,
                                 dps=20):
        """Calculate the hessian of the KL divergence (Fisher information metric) w.r.t.
        the theta_{ij} parameters replacing the spin i by sampling from j for the number
        of k votes in the majority.

        Use single step finite difference method to estimate Hessian.
        
        Parameters
        ----------
        hJ : ndarray, None
            Ising model parameters.
        dJ : ndarray, None
            Linear perturbations in parameter space corresponding to Hessian at given hJ.
            These can be calculuated using self.solve_linearized_perturbation().
        epsdJ : float, 1e-4
            Step size for taking linear perturbation wrt parameters.
        n_cpus : int, None
        check_stability : bool, False
        rtol : float, 1e-3
            Relative tolerance for each entry in Hessian when checking stability.
        full_output : bool, False
        dps : int, 20
            
        Returns
        -------
        ndarray
            Hessian.
        int (optional)
            Error flag. 1 indicates rtol was exceeded. None indicates that no check was
            done.
        float (optional)
            Norm difference between hessian with step size eps and eps/2.
        """
        
        import mpmath as mp
        mp.mp.dps = dps

        mplog2_ = lambda x:mp.log(x)/mp.log(2)
        mplog2 = lambda x: list(map(mplog2_, x))
        n = self.n
        if hJ is None:
            hJ = self.hJ
        p = self.p2pk_high_prec(self.ising.p(hJ), self.coarseUix, self.coarseInvix)
        log2p = np.array(mplog2(p))
        if dJ is None:
            dJ = self.dJ

        def diag(i,
                 hJ=hJ,
                 ising=self.ising,
                 dJ=dJ,
                 p=p,
                 p2pk=self.p2pk_high_prec,
                 uix=self.coarseUix,
                 invix=self.coarseInvix):
            # round epsdJ_ to machine precision
            mxix = np.argmax(np.abs(dJ[i]))
            newhJ = hJ[mxix] + dJ[i][mxix]*epsdJ
            epsdJ_ = (newhJ - hJ[mxix]) / dJ[i][mxix] / 2
            if np.isnan(epsdJ_): return 0.

            newhJ = hJ + dJ[i]*epsdJ_
            modp = p2pk(ising.p(newhJ), uix, invix)
            dklplus = 2*(log2p-mplog2(modp)).dot(p)

            newhJ -= 2*dJ[i]*epsdJ_
            modp = p2pk(ising.p(newhJ), uix, invix)
            dklminus = 2*(log2p-mplog2(modp)).dot(p)

            return (dklplus+dklminus) / 2 / epsdJ_**2

        # theta_j+del) to second order.
        def off_diag(args,
                     hJ=hJ,
                     ising=self.ising,
                     p2pk=self.p2pk_high_prec,
                     dJ=dJ,
                     p=p,
                     uix=self.coarseUix,
                     invix=self.coarseInvix):
            i, j = args
            
            # round epsdJ_ to machine precision
            mxix = np.argmax(np.abs(dJ[i]+dJ[j]))
            newhJ = hJ[mxix] + (dJ[i][mxix]+dJ[j][mxix])*epsdJ
            epsdJ_ = (newhJ - hJ[mxix]) / (dJ[i][mxix]+dJ[j][mxix]) / 2
            if np.isnan(epsdJ_): return 0.

            newhJ = hJ + (dJ[i]+dJ[j])*epsdJ_
            modp = p2pk(ising.p(newhJ), uix, invix)
            dklplus = (log2p-mplog2(modp)).dot(p)

            newhJ -= 2*(dJ[i]+dJ[j])*epsdJ_
            modp = p2pk(ising.p(newhJ), uix, invix)
            dklminus = (log2p-mplog2(modp)).dot(p)
            
            return (dklplus+dklminus) / 2 / epsdJ_**2

        hess = np.zeros((len(dJ),len(dJ)))
        if (not n_cpus is None) and n_cpus<=1:
            for i in range(len(dJ)):
                hess[i,i] = diag(i)
            for i,j in combinations(range(len(dJ)),2):
                hess[i,j] = off_diag((i,j))
        else:
            hess[np.eye(len(dJ))==1] = self.pool.map(diag, range(len(dJ)))
            hess[np.triu_indices_from(hess,k=1)] = self.pool.map(off_diag, combinations(range(len(dJ)),2))

        # subtract off linear terms to get Hessian (and not just cross derivative)
        hess[np.triu_indices_from(hess,k=1)] -= np.array([hess[i,i]/2+hess[j,j]/2
                                                        for i,j in combinations(range(len(dJ)),2)])
        # fill in lower triangle
        hess += hess.T
        hess[np.eye(len(dJ))==1] /= 2
        
        assert ~np.isnan(hess).any()
        assert ~np.isinf(hess).any()

        if check_stability:
            hess2 = self._maj_curvature_high_prec(epsdJ=epsdJ/2,
                                                  check_stability=False,
                                                  hJ=hJ,
                                                  dJ=dJ,
                                                  n_cpus=n_cpus)
            err = (hess - hess2)*4/3
            if (np.abs(err/hess) > rtol).any():
                errflag = 1
                msg = ("Finite difference estimate has not converged with rtol=%f. "+
                       "May want to shrink epsdJ. Norm error %f.")
                print(msg%(rtol,np.linalg.norm(err)))
            else:
                errflag = 0
                msg = "Finite difference estimate converged with rtol=%f."
                print(msg%rtol)
        else:
            errflag = None
            err = None

        if not full_output:
            return hess
        return hess, errflag, err

    def hess_eig(self, hess,
                 orientation_vector=None,
                 imag_norm_threshold=1e-10,
                 iprint=True):
        """Get Hessian eigenvalues and eigenvectors corresponds to parameter combinations
        of max curvature. Return them nicely sorted and cleaned and oriented consistently.
        
        Parameters
        ----------
        hess : ndarray
        orientation_vector : ndarray, None
            Vector along which to orient all vectors so that they are consistent with
            sign. By default, it is set to the sign of the first entry in the vector.
        imag_norm_threshold : float, 1e-10
        iprint : bool, True
        
        Returns
        -------
        ndarray
            Eigenvalues.
        ndarray
            Eigenvectors in cols.
        """
        
        if orientation_vector is None:
            orientation_vector = np.zeros(len(self.dJ))
            orientation_vector[0] = 1.

        eigval, eigvec = np.linalg.eig(hess)
        if (np.linalg.norm(eigval.imag)>imag_norm_threshold or
            np.linalg.norm(eigvec.imag[:,:10]>imag_norm_threshold)):
            print("Imaginary components are significant.")
        eigval = eigval.real
        eigvec = eigvec.real

        # orient all vectors along same direction
        eigvec *= np.sign(eigvec.T.dot(orientation_vector))[None,:]
        
        # sort by largest eigenvalues
        sortix = np.argsort(eigval)[::-1]
        eigval = eigval[sortix]
        eigvec = eigvec[:,sortix]
        # orient along direction of mean of individual means change
        eigvec *= np.sign(eigvec[:self.n,:].mean(0))[None,:]
        if iprint and (eigval<0).any():
            print("There are negative eigenvalues.")
            print()
        
        return eigval, eigvec

    def hess_eig2dJ(self, eigvec, dJ=None):
        if dJ is None:
            dJ = self.dJ
        return dJ.T.dot(eigvec)

    def component_subspace_dlogpk(self, hess, eps=1e-5):
        """Rate of change in log[p(k)] when moving along the principal mode of each
        component's subspace.

        See "2019-08-01 detail about CAA 99's affect on p(k).ipynb"

        Parameters
        ----------
        hess : ndarray
        eps : float, 1e-5

        Returns
        -------
        list of ndarray
            Each vector specifies rate of change in p(k) ordered where the number of
            voters in the majority decreases by one voter at a time.
        """
        
        from .influence import block_subspace_eig
        from coniii.utils import define_ising_helper_functions
        calc_e, calc_observables, _ = define_ising_helper_functions()
        n = self.n
        dlogp = []
        
        for ix in range(n):
            # iterate over components whose subspaces we explore
            # subspace eigenvector
            eigval, eigvec = block_subspace_eig(hess, n-1)

            v = eigvec[ix][:,0].real  # take principal eigenvector
            dE = calc_e(self.allStates.astype(np.int64), v.dot(self.dJ[ix*(n-1):(ix+1)*(n-1)])/(n-1))*eps
            E = np.log(self.p)
            pplus = np.exp(E+dE - fast_logsumexp(E+dE)[0])  # modified probability distribution
            pminus = np.exp(E-dE - fast_logsumexp(E-dE)[0])  # modified probability distribution

            pkplusdE = np.zeros(n//2+1)
            pkminusdE = np.zeros(n//2+1)
            for k in range(n//2+1):
                pkplusdE[k] = pplus[np.abs(self.allStates.sum(1))==(n-k*2)].sum()
                pkminusdE[k] = pminus[np.abs(self.allStates.sum(1))==(n-k*2)].sum()
            dlogp.append( (np.log2(pkplusdE) - np.log2(pkminusdE))/(2*eps) )
        return dlogp

    def __get_state__(self):
        # always close multiprocess pool when pickling
        if 'pool' in self.__dict__.keys():
            self.pool.close()
            del self.pool

        return {'n':self.n,
                'h':self.hJ[:self.n],
                'J':self.hJ[self.n:],
                'dJ':self.dJ,
                'eps':self.eps,
                'n_cpus':self.n_cpus}

    def __set_state__(self, state_dict):
        self.__init__(state_dict['n'], state_dict['h'], state_dict['J'], state_dict['eps'],
                      precompute=False,
                      n_cpus=state_dict.get('n_cpus',None))
        self.dJ = state_dict['dJ']
#end Magnetization


class MagnetizationConstant(Magnetization):
    """Perturbation of local magnetizations one at a time keeping fixed the amount of
    perturbation (this is akin to replacing only states that are contrary to the objective
    direction.
    """
    def observables_after_perturbation(self, i, eps=None):
        """Perturb all specified spin by forcing its magnetization by eps.
        
        Parameters
        ----------
        i : int
        eps : float, None

        Returns
        -------
        ndarray
            Observables <si> and <sisj> after perturbation.
        bool
            If True, made the specified spin point up +1. If False, made it point down -1.
        """
        
        if not hasattr(i,'__len__'):
            i = (i,)
        if not hasattr(eps,'__len__'):
            eps = eps or self.eps
            eps = [eps]*len(i)
        n = self.n
        si = self.sisj[:n]
        sisj = self.sisj[n:]
        
        # try perturbing up first
        siNew = si.copy()
        sisjNew = sisj.copy()
        perturb_up = True
        for i_,eps_ in zip(i,eps):
            # observables after perturbations
            jit_observables_after_perturbation_plus_mean(n, siNew, sisjNew, i_, eps_)
        # if we've surpassed the allowed values for correlations then try perturbing down
        # there is no check to make sure this perturbation doesn't lead to impossible values
        #if (np.abs(siNew)>1).any() or (np.abs(sisjNew)>1).any():
        #    siNew = si.copy()
        #    sisjNew = sisj.copy()
        #    perturb_up = False
        #    for i_,eps_ in zip(i,eps):
        #        # observables after perturbations
        #        jit_observables_after_perturbation_minus_mean(n, siNew, sisjNew, i_, eps_)

        return np.concatenate((siNew, sisjNew)), perturb_up
   
    def solve_linearized_perturbation(self, iStar,
                                      p=None,
                                      sisj=None,
                                      full_output=False,
                                      eps=None,
                                      check_stability=True,
                                      method='inverse'):
        """Consider a perturbation to a single spin.
        
        Parameters
        ----------
        iStar : int
        p : ndarray, None
        sisj : ndarray, None
        full_output : bool, False
        eps : float, None
        check_stability : bool, False
        method : str, 'inverse'
            Can be 'inverse' or 'lstsq'

        Returns
        -------
        ndarray
            dJ
        int
            Error flag. Returns 0 by default. 1 means badly conditioned matrix A.
        tuple (optional)
            (A,C)
        """
        
        eps = eps or self.eps
        n = self.n
        if p is None:
            p = self.p
        if sisj is None:
            si = self.sisj[:n]
            sisj = self.sisj[n:]
        else:
            si = sisj[:n]
            sisj = sisj[n:]
        A = np.zeros((n+n*(n-1)//2, n+n*(n-1)//2))
        C, perturb_up = self.observables_after_perturbation(iStar, eps=eps)
        
        # mean constraints
        for i in range(n):
            for k in range(n):
                if i==k:
                    A[i,i] = 1 - C[i]*si[i]
                else:
                    if i<k:
                        ikix = unravel_index((i,k),n)
                    else:
                        ikix = unravel_index((k,i),n)
                    A[i,k] = sisj[ikix] - C[i]*si[k]

            for klcount,(k,l) in enumerate(combinations(range(n),2)):
                A[i,n+klcount] = self.triplets[(i,k,l)].dot(p) - C[i]*sisj[klcount]
        
        # pair constraints
        for ijcount,(i,j) in enumerate(combinations(range(n),2)):
            for k in range(n):
                A[n+ijcount,k] = self.triplets[(i,j,k)].dot(p) - C[n+ijcount]*si[k]
            for klcount,(k,l) in enumerate(combinations(range(n),2)):
                A[n+ijcount,n+klcount] = self.quartets[(i,j,k,l)].dot(p) - C[n+ijcount]*sisj[klcount]
    
        C -= self.sisj
        if method=='inverse':
            # factor out linear dependence on eps
            try:
                dJ = np.linalg.solve(A,C)/eps
            except np.linalg.LinAlgError:
                dJ = np.zeros(C.size)+np.nan
        else:
            dJ = np.linalg.lstsq(A,C)[0]/eps
        # Since default is to perturb down
        if not perturb_up:
            dJ *= -1

        if check_stability:
            # double epsilon and make sure solution does not change by a large amount
            dJtwiceEps, errflag = self.solve_linearized_perturbation(iStar,
                                                                     eps=eps/2,
                                                                     check_stability=False,
                                                                     p=p,
                                                                     sisj=np.concatenate((si,sisj)))
            # print if relative change is more than .1% for any entry
            relerr = np.log10(np.abs(dJ-dJtwiceEps))-np.log10(np.abs(dJ))
            if (relerr>-3).any():
                print("Unstable solution. Recommend shrinking eps. %E"%(10**relerr.max()))
                   
        if np.linalg.cond(A)>1e15:
            warn("A is badly conditioned.")
            errflag = 1
        else:
            errflag = 0
        if full_output:
            return dJ, errflag, (A, C)
        return dJ, errflag
#end Magnetizationa


class Coupling(Magnetization):
    """Perturbation that increases correlation between pairs of spins.
    """
    def compute_dJ(self, p=None, sisj=None):
        # precompute linear change to parameters for small perturbation
        dJ = np.zeros((self.n*(self.n-1), self.n+(self.n-1)*self.n//2))
        counter = 0
        for i in range(self.n):
            for a in np.delete(range(self.n),i):
                dJ[counter], errflag = self.solve_linearized_perturbation(i, a, p=p, sisj=sisj)
                counter += 1
        return dJ

    @staticmethod
    def _observables_after_perturbation_up(si, sisj, i, a, eps):
        n = len(si)

        si[i] = 1 - eps*(si[i] - si[a])

        for j in delete(list(range(n)),i):
            if i<j:
                ijix = unravel_index((i,j),n)
            else:
                ijix = unravel_index((j,i),n)

            if j==a:
                sisj[ijix] = 1 - eps*(sisj[ijix] - 1)
            else:
                if j<a:
                    jaix = unravel_index((j,a),n)
                else:
                    jaix = unravel_index((a,j),n)
                sisj[ijix] = 1 - eps*(sisj[ijix] - sisj[jaix])
    
    @staticmethod
    def _observables_after_perturbation_down(si, sisj, i, a, eps):
        n = len(si)

        si[i] = 1 - eps*(si[i] + si[a])

        for j in delete(list(range(n)),i):
            if i<j:
                ijix = unravel_index((i,j),n)
            else:
                ijix = unravel_index((j,i),n)

            if j==a:
                sisj[ijix] = 1 - eps*(sisj[ijix] + 1)
            else:
                if j<a:
                    jaix = unravel_index((j,a),n)
                else:
                    jaix = unravel_index((a,j),n)
                sisj[ijix] = 1 - eps*(sisj[ijix] + sisj[jaix])

    def observables_after_perturbation(self, i, a, eps=None):
        """Make spin index i more like spin a by eps. Perturb the corresponding mean and
        the correlations with other spins j.
        
        Parameters
        ----------
        i : int
            Spin being perturbed.
        a : int
            Spin to mimic.
        eps : float, None

        Returns
        -------
        ndarray
            Observables <si> and <sisj> after perturbation.
        """
        
        if not hasattr(i,'__len__'):
            i = (i,)
        if not hasattr(a,'__len__'):
            a = (a,)
        for (i_,a_) in zip(i,a):
            assert i_!=a_
        if not hasattr(eps,'__len__'):
            eps = eps or self.eps
            eps = [eps]*len(i)
        n = self.n
        si = self.sisj[:n]
        sisj = self.sisj[n:]

        # observables after perturbations
        siNew = si.copy()
        sisjNew = sisj.copy()
        
        for i_,a_,eps_ in zip(i,a,eps):
            jit_observables_after_perturbation_plus(n, siNew, sisjNew, i_, a_, eps_)

        return np.concatenate((siNew, sisjNew))
    
    def _solve_linearized_perturbation_tester(self, iStar, aStar):
        """
        ***FOR DEBUGGING ONLY***
        
        Consider a perturbation to a single spin.
        
        Parameters
        ----------
        iStar : int
        full_output : bool, False

        Returns
        -------
        """
        
        n = self.n
        p = self.p
        C = self.observables_after_perturbation(iStar, aStar)
        
        from coniii.solvers import Enumerate
        solver = Enumerate(np.ones((1,n)))
        return (solver.solve(constraints=C)-self.hJ)/self.eps
    
    def solve_linearized_perturbation(self, *args, **kwargs):
        """Wrapper for automating search for best eps value for given perturbation.
        """
        
        # settings
        epsChangeFactor = 10
        
        # check whether error increases or decreases with eps
        eps0 = kwargs.get('eps', self.eps)
        kwargs['check_stability'] = True
        kwargs['full_output'] = True
        
        dJ, errflag, (A,C), relerr = self._solve_linearized_perturbation(*args, **kwargs)

        kwargs['eps'] = eps0*epsChangeFactor
        dJUp, errflagUp, _, relerrUp = self._solve_linearized_perturbation(*args, **kwargs)

        kwargs['eps'] = eps0/epsChangeFactor
        dJDown, errflagDown, _, relerrDown = self._solve_linearized_perturbation(*args, **kwargs)
        
        # if changing eps doesn't help, just return estimate at current eps
        if relerr.max()<relerrUp.max() and relerr.max()<relerrDown.max():
            return dJ, errflag
        
        # if error decreases more sharpy going down
        if relerrDown.max()<=relerrUp.max():
            epsChangeFactor = 1/epsChangeFactor
            prevdJ, errflag, prevRelErr = dJDown, errflagDown, relerrDown
        # if error decreases more sharpy going up, no need to change eps
        else:
            prevdJ, errflag, prevRelErr = dJUp, errflagUp, relerrUp
        
        # decrease/increase eps til error starts increasing
        converged = False
        while (not converged) and errflag:
            kwargs['eps'] *= epsChangeFactor
            dJ, errflag, (A,C), relerr = self._solve_linearized_perturbation(*args, **kwargs)
            if errflag and relerr.max()<prevRelErr.max():
                prevdJ = dJ
                prevRelErr = relerr
            else:
                converged = True
        
        return dJ, errflag

    def _solve_linearized_perturbation(self, iStar, aStar,
                                      p=None,
                                      sisj=None,
                                      full_output=False,
                                      eps=None,
                                      check_stability=True,
                                      disp=False):
        """Consider a perturbation to a single spin.
        
        Parameters
        ----------
        iStar : int
        aStar : int
        p : ndarray, None
        sisj : ndarray, None
        full_output : bool, False
        eps : float, None
        check_stability : bool, False

        Returns
        -------
        ndarray
            dJ
        int
            Error flag. Returns 0 by default. 1 means badly conditioned matrix A.
        tuple (optional)
            (A,C)
        float (optional)
            Relative error to log10.
        """
        
        eps = eps or self.eps
        n = self.n
        if p is None:
            p = self.p
        if sisj is None:
            si = self.sisj[:n]
            sisj = self.sisj[n:]
        else:
            si = sisj[:n]
            sisj = sisj[n:]
        A = np.zeros((n+n*(n-1)//2, n+n*(n-1)//2))
        C = self.observables_after_perturbation(iStar, aStar, eps=eps)
        errflag = 0
        
        # mean constraints
        for i in range(n):
            for k in range(n):
                if i==k:
                    A[i,i] = 1 - C[i]*si[i]
                else:
                    if i<k:
                        ikix = unravel_index((i,k),n)
                    else:
                        ikix = unravel_index((k,i),n)
                    A[i,k] = sisj[ikix] - C[i]*si[k]

            for klcount,(k,l) in enumerate(combinations(range(n),2)):
                A[i,n+klcount] = self.triplets[(i,k,l)].dot(p) - C[i]*sisj[klcount]
        
        # pair constraints
        for ijcount,(i,j) in enumerate(combinations(range(n),2)):
            for k in range(n):
                A[n+ijcount,k] = self.triplets[(i,j,k)].dot(p) - C[n+ijcount]*si[k]
            for klcount,(k,l) in enumerate(combinations(range(n),2)):
                A[n+ijcount,n+klcount] = self.quartets[(i,j,k,l)].dot(p) - C[n+ijcount]*sisj[klcount]
    
        C -= self.sisj
        # factor out linear dependence on eps
        dJ = np.linalg.solve(A,C)/eps

        if check_stability:
            # double epsilon and make sure solution does not change by a large amount
            dJtwiceEps, errflag = self._solve_linearized_perturbation(iStar, aStar,
                                                                      p=p,
                                                                      sisj=np.concatenate((si,sisj)),
                                                                      eps=eps/2,
                                                                      check_stability=False)
            # print if relative change is more than .1% for any entry
            relerr = np.log10(np.abs(dJ-dJtwiceEps))-np.log10(np.abs(dJ))
            if (relerr>-3).any():
                if disp:
                    print("Unstable solution. Recommend shrinking eps. Max err=%E"%(10**relerr.max()))
                errflag = 2
        
        if np.linalg.cond(A)>1e15:
            warn("A is badly conditioned.")
            # this takes precedence over relerr over threshold
            errflag = 1

        if full_output:
            if check_stability:
                return dJ, errflag, (A, C), relerr
            return dJ, errflag, (A, C)
        return dJ, errflag
#end Couplings


class Coupling3(Coupling):
    """Pairwise perturbations tweaked for ternary states like C. elegans."""
    def __init__(self, n,
                 h=None,
                 J=None,
                 eps=1e-7,
                 precompute=True,
                 n_cpus=None,
                 n_samples=10_000_000):
        """
        Parameters
        ----------
        n : int
        h : ndarray, None
        J : ndarray, None
        eps : float, 1e-7
        precompute : bool, True
        n_cpus : int, None
        n_samples : int, 10_000_000
            Number of samples for Metropolis sampling.
        """

        from coniii.utils import xpotts_states

        assert n>1 and 0<eps<1e-2
        assert (h[2*n:3*n]==0).all()
        assert h.size==3*n and J.size==n*(n-1)//2

        self.n = n
        self.kStates = 3
        self.eps = eps
        self.hJ = np.concatenate((h,J))
        self.n_cpus = n_cpus

        self.ising = LargePotts3((h,J), n_samples)
        self.sisj = np.concatenate(self.ising.corr[:2])
        self.p = self.ising.p
        self.allStates = self.ising.states.astype(np.int8)
        _, self.coarseInvix = np.unique(np.abs(self.allStates.sum(1)), return_inverse=True)
        self.coarseUix = np.unique(self.coarseInvix)

        # cache triplet and quartet products
        self._triplets_and_quartets() 
    
        if precompute:
            self.dJ = self.compute_dJ()
        else:
            self.dJ = None

    def _triplets_and_quartets(self):
        from itertools import product

        n = self.n
        kStates = self.kStates
        self.pairs = {}
        self.triplets = {}
        self.quartets = {}
        allStates = np.vstack(list(xpotts_states(n, kStates))).astype(int)

        # <d_{i,gammai} * d_{j,gammaj}> where i<j
        for i,j in combinations(range(n),2):
            for gammai,gammaj in product(range(kStates),range(kStates)):
                ix = (allStates[:,i]==gammai)&(allStates[:,j]==gammaj)
                self.pairs[(gammai,i,gammaj,j)] = ix

        # triplets that matter are when one spin is in a particular state and the
        # remaining two agree with each other
        for gamma in range(kStates):
            for i in range(n):
                for j,k in combinations(range(n),2):
                    ix = (allStates[:,i]==gamma)&(allStates[:,j]==allStates[:,k])
                    self.triplets[(gamma,i,j,k)] = ix
        # quartets that matter are when the first pair are the same and the second pair
        # are the same
        for i,j in combinations(range(n),2):
            for k,l in combinations(range(n),2):
                ix1 = allStates[:,i]==allStates[:,j]
                ix2 = allStates[:,k]==allStates[:,l]
                self.quartets[(i,j,k,l)] = ix1&ix2

    def compute_dJ(self, p=None, sisj=None, n_cpus=0):
        """Compute linear change to parameters for small perturbation.
        
        Parameters
        ----------
        p : ndarray, None
        sisj : ndarray, None
        n_cpus : int, 0
            This is not any faster with multiprocessing.

        Returns
        -------
        dJ : ndarray
            (n_perturbation_parameters, n_maxent_parameters)
        """

        n_cpus = n_cpus or self.n_cpus

        def wrapper(params):
            i, a = params
            return self.solve_linearized_perturbation(i, a, p=p, sisj=sisj)[0]

        def args():
            for i in range(self.n):
                for a in np.delete(range(self.n),i):
                    yield (i,a)

        if self.n_cpus is None or self.n_cpus>1:
            try: 
                # don't use all the cpus since lin alg calculations will be slower
                pool = Pool(self.n_cpus or cpu_count()//2)
                dJ = np.vstack(( pool.map(wrapper, args()) ))
            finally:
                pool.close()
        else:
            dJ = np.zeros((self.n*(self.n-1), 3*self.n+(self.n-1)*self.n//2))
            for counter,(i,a) in enumerate(args):
                dJ[counter] = wrapper((i,a))
        return dJ
    
    def observables_after_perturbation(self, i, a, eps=None):
        """Make spin index i more like spin a by eps. Perturb the corresponding mean and
        the correlations with other spins j.
        
        Parameters
        ----------
        i : int
            Spin being perturbed.
        a : int
            Spin to mimic.
        eps : float, None

        Returns
        -------
        ndarray
            Observables <si> and <sisj> after perturbation.
        bool
            perturb_up
        """
        
        if not hasattr(i,'__len__'):
            i = (i,)
        if not hasattr(a,'__len__'):
            a = (a,)
        for (i_,a_) in zip(i,a):
            assert i_!=a_
        if not hasattr(eps,'__len__'):
            eps = eps or self.eps
            eps = [eps]*len(i)
        n = self.n
        si = self.sisj[:n*3]
        sisj = self.sisj[3*n:]

        # observables after perturbations
        siNew = si.copy()
        sisjNew = sisj.copy()
        
        for i_,a_,eps_ in zip(i,a,eps):
            jit_observables_after_perturbation_minus(n, siNew, sisjNew, i_, a_, eps_)

        return np.concatenate((siNew, sisjNew)), True
   
    def _maj_curvature(self,
                       hJ=None,
                       dJ=None,
                       epsdJ=1e-7,
                       check_stability=False,
                       rtol=1e-3,
                       full_output=False,
                       calc_off_diag=True,
                       calc_diag=True,
                       iprint=True):
        """Calculate the hessian of the KL divergence (Fisher information metric) w.r.t.
        the theta_{ij} parameters replacing the spin i by sampling from j for the number
        of k votes in the majority.

        Use single step finite difference method to estimate Hessian.
        
        Parameters
        ----------
        hJ : ndarray, None
            Ising model parameters.
        dJ : ndarray, None
            Linear perturbations in parameter space corresponding to Hessian at given hJ.
            These can be calculuated using self.solve_linearized_perturbation().
        epsdJ : float, 1e-4
            Step size for taking linear perturbation wrt parameters.
        check_stability : bool, False
        rtol : float, 1e-3
            Relative tolerance for each entry in Hessian when checking stability.
        full_output : bool, False
        calc_off_diag : bool, True
        calc_diag : bool, True
        iprint : bool, True
            
        Returns
        -------
        ndarray
            Hessian.
        int (optional)
            Error flag. 1 indicates rtol was exceeded. None indicates that no check was
            done.
        float (optional)
            Norm difference between hessian with step size eps and eps/2.
        """

        n = self.n
        if hJ is None:
            hJ = self.hJ
        E = calc_all_energies(n, self.kStates, hJ)
        logZ = fast_logsumexp(-E)[0]
        logsumEk = self.logp2pk(E, self.coarseUix, self.coarseInvix)
        p = np.exp(logsumEk - logZ)
        assert np.isclose(p.sum(),1), p.sum()
        if dJ is None:
            dJ = self.dJ
            assert self.dJ.shape[1]==(self.kStates*n+n*(n-1)//2)
        if iprint:
            print('Done with preamble.')

        # diagonal entries of hessian
        def diag(i, hJ=hJ, dJ=dJ, p=self.p, pk=p, logp2pk=self.logp2pk,
                 uix=self.coarseUix, invix=self.coarseInvix,
                 n=self.n, E=E, logZ=logZ, kStates=self.kStates):
            # round eps step to machine precision
            mxix = np.abs(dJ[i]).argmax()
            newhJ = hJ[mxix] + dJ[i][mxix]*epsdJ
            epsdJ_ = (newhJ-hJ[mxix]) / dJ[i][mxix]
            if np.isnan(epsdJ_): return 0.
            correction = calc_all_energies(n, kStates, dJ[i]*epsdJ_)
            correction = np.array([correction[invix==ix].dot(p[invix==ix])/p[invix==ix].sum()
                                   for ix in range(len(uix))])
            num = ((correction.dot(pk) - correction)**2).dot(pk)
            dd = num / np.log(2) / epsdJ_**2
            if iprint and np.isnan(dd):
                print('nan for diag', i, epsdJ_)
            
            return dd

        # off-diagonal entries of hessian
        def off_diag(args, hJ=hJ, dJ=dJ, p=self.p, pk=p, logp2pk=self.logp2pk,
                     uix=self.coarseUix, invix=self.coarseInvix,
                     n=self.n, E=E, logZ=logZ, kStates=self.kStates):
            i, j = args
            
            # round eps step to machine precision
            mxix = np.abs(dJ[i]).argmax()
            newhJ = hJ[mxix] + dJ[i][mxix]*epsdJ
            epsdJi = (newhJ - hJ[mxix])/dJ[i][mxix]/2
            if np.isnan(epsdJi): return 0.
            correction = calc_all_energies(n, kStates, dJ[i]*epsdJi)
            correctioni = np.array([correction[invix==ix].dot(p[invix==ix])/p[invix==ix].sum()
                                    for ix in range(len(uix))])

            # round eps step to machine precision
            mxix = np.abs(dJ[j]).argmax()
            newhJ = hJ[mxix] + dJ[j][mxix]*epsdJ
            epsdJj = (newhJ - hJ[mxix])/dJ[j][mxix]/2
            if np.isnan(epsdJj): return 0.
            correction = calc_all_energies(n, kStates, dJ[j]*epsdJj)
            correctionj = np.array([correction[invix==ix].dot(p[invix==ix])/p[invix==ix].sum()
                                    for ix in range(len(uix))])

            num = ((correctioni.dot(pk) - correctioni)*(correctionj.dot(pk) - correctionj)).dot(pk)
            dd = num / np.log(2) / (epsdJi * epsdJj)
            if iprint and np.isnan(dd):
                print('nan for off diag', args, epsdJi, epsdJj)
            return dd
        
        hess = np.zeros((len(dJ),len(dJ)))
        if not 'pool' in self.__dict__.keys():
            warn("Not using multiprocess can lead to excessive memory usage.")
            if calc_diag:
                for i in range(len(dJ)):
                    hess[i,i] = diag(i)
                if iprint:
                    print("Done with diag.")
            if calc_off_diag:
                for i,j in combinations(range(len(dJ)),2):
                    hess[i,j] = off_diag((i,j))
                    if iprint:
                        print("Done with off diag (%d,%d)."%(i,j))
                if iprint:
                    print("Done with off diag.")
        else:
            if calc_diag:
                hess[np.eye(len(dJ))==1] = self.pool.map(diag, range(len(dJ)))
                if iprint:
                    print("Done with diag.")
            if calc_off_diag:
                hess[np.triu_indices_from(hess,k=1)] = self.pool.map(off_diag,
                                                                     combinations(range(len(dJ)),2))
                if iprint:
                    print("Done with off diag.")

        if calc_off_diag:
            # fill in lower triangle
            hess += hess.T
            hess[np.eye(len(dJ))==1] /= 2

        # check for precision problems
        assert ~np.isnan(hess).any(), hess
        assert ~np.isinf(hess).any(), hess

        if check_stability:
            hess2 = self._maj_curvature(epsdJ=epsdJ/2,
                                        check_stability=False,
                                        iprint=iprint,
                                        hJ=hJ,
                                        dJ=dJ,
                                        calc_diag=calc_diag,
                                        calc_off_diag=calc_off_diag)
            err = hess - hess2
            if (np.abs(err/hess) > rtol).any():
                errflag = 1
                if iprint:
                    msg = ("Finite difference estimate has not converged with rtol=%f. "+
                           "May want to shrink epsdJ. Norm error %f.")
                    print(msg%(rtol,np.linalg.norm(err)))
            else:
                errflag = 0
                if iprint:
                    msg = "Finite difference estimate converged with rtol=%f."
                    print(msg%rtol)
        else:
            errflag = None
            err = None

        if not full_output:
            return hess
        return hess, errflag, err

    def _test_maj_curvature(self):
        n = self.n
        hJ = self.hJ
        E = calc_all_energies(n, self.kStates, hJ)
        logZ = fast_logsumexp(-E)[0]
        logsumEk = self.logp2pk(E, self.coarseUix, self.coarseInvix)
        p = np.exp(logsumEk - logZ)
        dJ = self.dJ

        # diagonal entries of hessian
        def diag(i, eps, hJ=hJ, dJ=dJ, p=p, logp2pk=self.logp2pk,
                 uix=self.coarseUix, invix=self.coarseInvix,
                 n=self.n, E=E, logZ=logZ, kStates=self.kStates):
            # round eps step to machine precision
            mxix = np.abs(dJ[i]).argmax()
            newhJ = hJ[mxix] + dJ[i][mxix]*eps
            eps = (newhJ-hJ[mxix]) / dJ[i][mxix]
            if np.isnan(eps): return 0.
            correction = calc_all_energies(n, kStates, dJ[i]*eps)
            
            # forward step
            Enew = E+correction
            modlogsumEkplus = logp2pk(Enew, uix, invix)
            #Zkplus = fast_logsumexp(-Enew)[0]
            
            # backwards step
            Enew = E-correction
            modlogsumEkminus = logp2pk(Enew, uix, invix)
            #Zkminus = fast_logsumexp(-Enew)[0]

            num = (logsumEk - modlogsumEkplus)**2
            ddplus = num.dot(p) / np.log(2) / eps**2

            num = (logsumEk - modlogsumEkminus)**2
            ddminus = num.dot(p) / np.log(2) / eps**2

            #num_ = 2*(logsumEk - logZ) + (Zkplus - modlogsumEkplus) + (Zkminus - modlogsumEkminus)
            #print( num_.dot(p) / np.log(2) / 2 / eps**2 )
            return ddplus, ddplus-ddminus
        return diag
  
    def _solve_linearized_perturbation_tester(self, iStar, aStar):
        """
        ***FOR DEBUGGING ONLY***

        Parameters
        ----------
        iStar : int
        aStar : int

        Returns
        -------
        ndarray
            Estimated linear change in maxent parameters.
        """
        
        n = self.n
        k = self.kStates
        p = self.p
        C = self.observables_after_perturbation(iStar, aStar)[0]
        assert k==3, "Only handles k=3."

        from coniii.solvers import Enumerate
        from coniii.models import TernaryIsing
        model = TernaryIsing([np.zeros(k*n), np.zeros(n*(n-1)//2)])
        calc_observables = define_ternary_helper_functions()[1]
        solver = Enumerate(np.ones((1,n)),
                           model=model,
                           calc_observables=calc_observables)
        
        # hybr solver seems to work more consistently than default krylov
        soln = solver.solve(constraints=C,
                            initial_guess=self.hJ,
                            full_output=True,
                            scipy_solver_kwargs={'method':'hybr', 'tol':1e-12})
        soln = soln[0]
        # remove translational offset for first set of fields
        soln[:n*k] -= np.tile(soln[:n], k)
        return (soln - self.hJ)/(self.eps)

    def _solve_linearized_perturbation(self, iStar, kStar,
                                       p=None,
                                       sisj=None,
                                       full_output=False,
                                       eps=None,
                                       check_stability=True,
                                       disp=False):
        """Consider a perturbation to a single spin to make it more likely be in a
        particular state. Remember that this assumes that the fields for the first state
        are set to zero to remove the translation symmetry.
        
        Parameters
        ----------
        iStar : int
        kStar : int
        p : ndarray, None
        sisj : ndarray, None
        full_output : bool, False
        eps : float, None
        check_stability : bool, False

        Returns
        -------
        ndarray
            dJ
        int
            Error flag. Returns 0 by default. 1 means badly conditioned matrix A.
        tuple (optional)
            (A,C)
        float (optional)
            Relative error to log10.
        """
        
        eps = eps or self.eps
        n = self.n
        kStates = self.kStates
        if p is None:
            p = self.p
        if sisj is None:
            si = self.sisj[:n*kStates]
            sisj = self.sisj[kStates*n:]
        else:
            si = sisj[:kStates*n]
            sisj = sisj[kStates*n:]
        # matrix that will be multiplied by the vector of canonical parameter perturbations
        A = np.zeros((kStates*n+n*(n-1)//2, (kStates-1)*n+n*(n-1)//2))
        C, perturb_up = self.observables_after_perturbation(iStar, kStar, eps=eps)
        errflag = 0
        
        # mean constraints (remember that A does not include changes in first set of fields)
        for i in range(kStates*n):
            for j in range(n,kStates*n):
                if i==j:
                    A[i,j-n] = si[i] - C[i]*si[j]
                # if they're in different states but the same spin
                elif (i%n)==(j%n):
                    A[i,j-n] = -C[i]*si[j]
                else:
                    if (i%n)<(j%n):
                        A[i,j-n] = self.pairs[(i//n,i%n,j//n,j%n)].dot(p) - C[i]*si[j]
                    else:
                        A[i,j-n] = self.pairs[(j//n,j%n,i//n,i%n)].dot(p) - C[i]*si[j]

            for klcount,(k,l) in enumerate(combinations(range(n),2)):
                A[i,(kStates-1)*n+klcount] = self.triplets[(i//n,i%n,k,l)].dot(p) - C[i]*sisj[klcount]
        
        # pair constraints
        for ijcount,(i,j) in enumerate(combinations(range(n),2)):
            for k in range(kStates*n):
                A[kStates*n+ijcount,k-n] = (self.triplets[(k//n,k%n,i,j)].dot(p) -
                                            C[kStates*n+ijcount]*si[k])
            for klcount,(k,l) in enumerate(combinations(range(n),2)):
                A[kStates*n+ijcount,(kStates-1)*n+klcount] = (self.quartets[(i,j,k,l)].dot(p) -
                                                              C[kStates*n+ijcount]*sisj[klcount])
        C -= self.sisj
        # factor out linear dependence on eps
        dJ = np.linalg.lstsq(A, C, rcond=None)[0]/eps
        # put back in fields that we've fixed
        dJ = np.concatenate((np.zeros(n), dJ))

        if check_stability:
            # double epsilon and make sure solution does not change by a large amount
            dJtwiceEps, errflag = self._solve_linearized_perturbation(iStar, kStar,
                                                                      p=p,
                                                                      sisj=np.concatenate((si,sisj)),
                                                                      eps=eps/2,
                                                                      check_stability=False)
            # print if relative change is more than .1% for any entry
            relerr = np.log10(np.abs(dJ[n:]-dJtwiceEps[n:]))-np.log10(np.abs(dJ[n:]))
            if (relerr>-3).any():
                if disp:
                    print("Unstable solution. Recommend shrinking eps. Max err=%E"%(10**relerr.max()))
                errflag = 2
        
        if np.linalg.cond(A)>1e15:
            warn("A is badly conditioned.")
            # this takes precedence over relerr over threshold
            errflag = 1

        if full_output:
            if check_stability:
                return dJ, errflag, (A, C), relerr
            return dJ, errflag, (A, C)
        return dJ, errflag
 
    def __get_state__(self):
        # always close multiprocess pool when pickling
        if 'pool' in self.__dict__.keys():
            self.pool.close()
            del self.pool

        return {'n':self.n,
                'k':self.kStates,
                'h':self.hJ[:self.n*self.kStates],
                'J':self.hJ[self.n*self.kStates:],
                'dJ':self.dJ,
                'eps':self.eps,
                'n_cpus':self.n_cpus}

    def __set_state__(self, state_dict):
        self.__init__(state_dict['n'], state_dict['k'],
                      h=state_dict['h'],
                      J=state_dict['J'],
                      eps=state_dict['eps'],
                      precompute=False,
                      n_cpus=state_dict.get('n_cpus',None))
        self.dJ = state_dict['dJ']
#end Coupling3


# ============= #
# JIT functions #
# ============= #
@njit
def jit_observables_after_perturbation_plus(n, si, sisj, i, a, eps):
    osisj = sisj.copy()
    si[i] = si[i] - eps*(si[i] - si[a])

    for j in delete(list(range(n)),i):
        if i<j:
            ijix = unravel_index((i,j),n)
        else:
            ijix = unravel_index((j,i),n)

        if j==a:
            sisj[ijix] = sisj[ijix] - eps*(sisj[ijix] - 1)
        else:
            if j<a:
                jaix = unravel_index((j,a),n)
            else:
                jaix = unravel_index((a,j),n)
            sisj[ijix] = sisj[ijix] - eps*(sisj[ijix] - osisj[jaix])

@njit
def jit_observables_after_perturbation_minus(n, si, sisj, i, a, eps):
    osisj = sisj.copy()
    si[i] = si[i] - eps*(si[i] + si[a])

    for j in delete(list(range(n)),i):
        if i<j:
            ijix = unravel_index((i,j),n)
        else:
            ijix = unravel_index((j,i),n)

        if j==a:
            sisj[ijix] = sisj[ijix] - eps*(sisj[ijix] + 1)
        else:
            if j<a:
                jaix = unravel_index((j,a),n)
            else:
                jaix = unravel_index((a,j),n)
            sisj[ijix] = sisj[ijix] - eps*(sisj[ijix] + osisj[jaix])

@njit
def jit_observables_after_perturbation_plus_field(n, si, sisj, i, eps):
    si[i] = si[i] - eps*(si[i] - 1)

    for j in delete(list(range(n)),i):
        if i<j:
            ijix = unravel_index((i,j),n)
        else:
            ijix = unravel_index((j,i),n)

        sisj[ijix] = sisj[ijix] - eps*(sisj[ijix] - si[j])

@njit
def jit_observables_after_perturbation_minus_field(n, si, sisj, i, eps):
    si[i] = si[i] - eps*(si[i] + 1)

    for j in delete(list(range(n)),i):
        if i<j:
            ijix = unravel_index((i,j),n)
        else:
            ijix = unravel_index((j,i),n)

        sisj[ijix] = sisj[ijix] - eps*(sisj[ijix] + si[j])

@njit
def jit_observables_after_perturbation_plus_mean(n, si, sisj, i, eps):
    si[i] = (1-eps)*si[i] + eps

    for j in delete(list(range(n)),i):
        if i<j:
            ijix = unravel_index((i,j),n)
        else:
            ijix = unravel_index((j,i),n)

        sisj[ijix] = (1-eps)*sisj[ijix] + eps*si[j]

@njit
def jit_observables_after_perturbation_minus_mean(n, si, sisj, i, eps):
    si[i] = si[i] - eps

    for j in delete(list(range(n)),i):
        if i<j:
            ijix = unravel_index((i,j),n)
        else:
            ijix = unravel_index((j,i),n)

        sisj[ijix] = sisj[ijix] - eps*si[j]

@njit
def delete(X, i):
    """Return vector X with the ith element removed."""
    X_ = [0]
    X_.pop(0)
    for j in range(len(X)):
        if i!=j:
            X_.append(X[j])
    return X_

@njit
def factorial(x):
    f = 1.
    while x>0:
        f *= x
        x -= 1
    return f

@njit
def binom(n,k):
    return factorial(n)/factorial(n-k)/factorial(k)

@njit
def jit_all(x):
    for x_ in x:
        if not x_:
            return False
    return True

@njit
def unravel_index(ijk, n):
    """Unravel multi-dimensional index to flattened index but specifically for
    multi-dimensional analog of an upper triangular array (lower triangle indices are not
    counted).

    Parameters
    ----------
    ijk : tuple
        Raveled index to unravel.
    n : int
        System size.

    Returns
    -------
    ix : int
        Unraveled index.
    """
    
    if len(ijk)==1:
        raise Exception

    assert jit_all([ijk[i]<ijk[i+1] for i in range(len(ijk)-1)])
    assert jit_all([i<n for i in ijk])

    ix = np.sum(np.array([int(binom(n-1-i,len(ijk)-1)) for i in range(ijk[0])]))
    for d in range(1, len(ijk)-1):
        if (ijk[d]-ijk[d-1])>1:
            ix += np.sum(np.array([int(binom(n-i-1,len(ijk)-d-1)) for i in range(ijk[d-1]+1, ijk[d])]))
    ix += ijk[-1] -ijk[-2] -1
    return ix

@njit
def fast_sum(J, s):
    """Helper function for calculating energy in calc_e(). Iterates couplings J."""
    e = 0
    k = 0
    for i in range(len(s)-1):
        for j in range(i+1,len(s)):
            e += J[k]*s[i]*s[j]
            k += 1
    return e

@njit
def fast_sum_ternary(J, s):
    """Helper function for calculating energy in calc_e(). Iterates couplings J."""
    assert len(J)==(len(s)*(len(s)-1)//2)

    e = 0
    k = 0
    for i in range(len(s)-1):
        for j in range(i+1,len(s)):
            if s[i]==s[j]:
                e += J[k]
            k += 1
    return e

@njit("float64[:](int64,int64,float64[:])")
def calc_all_energies(n, k, params):
    """Calculate all the energies for the 2^n or 3^n states in model.
    
    Parameters
    ----------
    n : int
        Number of spins.
    k : int
        Number of distinct states.
    params : ndarray
        (h,J) vector

    Returns
    -------
    E : ndarray
        Energies of all given states.
    """
    
    e = np.zeros(k**n)
    s_ = np.zeros(n, dtype=np.int64)
    if k==2:
        for i,s in enumerate(xpotts_states(n, k)):
            for ix in range(n):
                if s[ix]=='0':
                    s_[ix] = -1
                else:
                    s_[ix] = 1
            e[i] -= fast_sum(params[n:], s_)
            e[i] -= np.sum(s_*params[:n])
    elif k==3:
        for i,s in enumerate(xpotts_states(n, k)):
            for ix in range(n):
                if s[ix]=='0':
                    s_[ix] = 0
                elif s[ix]=='1':
                    s_[ix] = 1
                elif s[ix]=='2':
                    s_[ix] = 2
                else:
                    raise Exception
                # fields
                e[i] -= params[ix+s_[ix]*n]
            e[i] -= fast_sum_ternary(params[n*k:], s_)
    else: raise NotImplementedError
    return e

def jit_spin_replace_transition_matrix(n, i, j, eps):
    rows = []
    cols = []
    vals = []
    for ix in range(2**n):
        s = bin(ix)[2:].zfill(n)
        if s[i]!=s[j]:
            rows.append(ix)
            cols.append(ix)
            vals.append(1-eps)
            
            if s[i]=='0':
                s = s[:i]+'1'+s[i+1:]
            else:
                s = s[:i]+'0'+s[i+1:]
            rows.append(int(s,2))
            cols.append(ix)
            vals.append(eps)
        else:
            rows.append(ix)
            cols.append(ix)
            vals.append(1.)
    return rows, cols, vals

@njit(cache=True)
def fast_sum(J,s):
    """Helper function for calculating energy in calc_e(). Iterates couplings J."""
    e = np.zeros((s.shape[0]))
    for n in range(s.shape[0]):
        k = 0
        for i in range(s.shape[1]-1):
            for j in range(i+1,s.shape[1]):
                e[n] += J[k]*s[n,i]*s[n,j]
                k += 1
    return e

@njit("float64[:](int8[:,:],float64[:])")
def calc_e(s, params):
    """
    Parameters
    ----------
    s : 2D ndarray
        state either {0,1} or {+/-1}
    params : ndarray
        (h,J) vector

    Returns
    -------
    E : ndarray
        Energies of all given states.
    """
    
    e = -fast_sum(params[s.shape[1]:],s)
    e -= np.sum(s*params[:s.shape[1]],1)
    return e

@njit(cache=True)
def jit_triplets_and_quartets(n, allStates):
    triplets = dict()
    quartets = dict()
    for i in range(n):
        for j,k in jit_pair_combination(n):
            triplets[(i,j,k)] = allStates[:,i]*allStates[:,j]*allStates[:,k]
    for i,j in jit_pair_combination(n):
        for k in range(n):
            triplets[(i,j,k)] = allStates[:,i]*allStates[:,j]*allStates[:,k]
        for k,l in jit_pair_combination(n):
            quartets[(i,j,k,l)] = allStates[:,i]*allStates[:,j]*allStates[:,k]*allStates[:,l]
    return triplets, quartets

@njit(cache=True)
def jit_pair_combination(x):
    for i in range(x-1):
        for j in range(i+1,x):
            yield i,j