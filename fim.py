# ============================================================================================ #
# Classes for calculating FIM on Ising model.
# Author : Eddie Lee, edlee@alumni.princeton.edu
# ============================================================================================ # 
import numpy as np
from numba import njit
from coniii.utils import *
from .utils import *
import importlib
from warnings import warn
from itertools import combinations
from coniii.enumerate import fast_logsumexp
from coniii.utils import define_ising_helper_functions
calc_e, _, _ = define_ising_helper_functions()
np.seterr(divide='ignore')



class IsingFisherCurvatureMethod1():
    """Perturbation of local magnetizations one at a time.
    """
    def __init__(self, n, h=None, J=None, eps=1e-7, precompute=True, n_cpus=None):
        """
        Parameters
        ----------
        n : int
        h : ndarray, None
        J : ndarray, None
        eps : float, 1e-7
        precompute : bool, True
        n_cpus : int, None
        """
        

        assert n>1 and 0<eps<.1
        self.n = n
        self.kStates = 2
        self.eps = eps
        self.hJ = np.concatenate((h,J))
        self.n_cpus = n_cpus

        self.ising = importlib.import_module('coniii.ising_eqn.ising_eqn_%d_sym'%n)
        self.sisj = self.ising.calc_observables(self.hJ)
        self.p = self.ising.p(self.hJ)
        self.allStates = bin_states(n, True).astype(int)
        self.coarseUix, self.coarseInvix = np.unique(np.abs(self.allStates.sum(1)), return_inverse=True)
        
        # cache triplet and quartet products
        self._triplets_and_quartets() 
    
        if precompute:
            self.dJ = self.compute_dJ()
        else:
            self.dJ = np.zeros((self.n,self.n+(self.n-1)*self.n//2))
    
    def _triplets_and_quartets(self):
        n = self.n
        self.triplets = {}
        self.quartets = {}
        for i in range(n):
            for j,k in combinations(range(n),2):
                self.triplets[(i,j,k)] = np.prod(self.allStates[:,(i,j,k)],1).astype(np.int8)
        for i,j in combinations(range(n),2):
            for k in range(n):
                self.triplets[(i,j,k)] = np.prod(self.allStates[:,(i,j,k)],1).astype(np.int8)
            for k,l in combinations(range(n),2):
                self.quartets[(i,j,k,l)] = np.prod(self.allStates[:,(i,j,k,l)],1).astype(np.int8)

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
        
        #for i_,eps_ in zip(i,eps):
        #    # observables after perturbations
        #    jit_observables_after_perturbation_plus_field(n, siNew, sisjNew, i_, eps_)
        for i_,eps_ in zip(i,eps):
            # observables after perturbations
            jit_observables_after_perturbation_minus_field(n, siNew, sisjNew, i_, eps_)
        perturb_up = False

        return np.concatenate((siNew, sisjNew)), perturb_up
   
    def _observables_after_perturbation_up(self, si, sisj, i, eps):
        """        
        Parameters
        ----------
        si : ndarray
        sisj : ndarray
        i : int
        eps : float
        """

        n = self.n
        
        # observables after perturbations
        si[i]  = (1-eps)*si[i] + eps

        for j in delete(list(range(n)),i):
            if i<j:
                ijix = unravel_index((i,j),n)
            else:
                ijix = unravel_index((j,i),n)
            sisj[ijix] = (1-eps)*sisj[ijix] + eps*si[j]

    def _observables_after_perturbation_down(self, si, sisj, i, eps):
        """        
        Parameters
        ----------
        si : ndarray
        sisj : ndarray
        i : int
        eps : float
        """

        n = self.n
        
        # observables after perturbations
        si[i]  = (1-eps)*si[i] - eps

        for j in delete(list(range(n)),i):
            if i<j:
                ijix = unravel_index((i,j),n)
            else:
                ijix = unravel_index((j,i),n)
            sisj[ijix] = (1-eps)*sisj[ijix] - eps*si[j]

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

        solver = Enumerate(n, calc_observables_multipliers=self.ising.calc_observables)
        if perturb_up:
            return (solver.solve(C)-self.hJ)/eps

        # account for sign of perturbation on fields
        dJ = -(solver.solve(C)-self.hJ)/eps
        return dJ

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

    def maj_curvature(self, *args, **kwargs):
        """Wrapper for _dkl_curvature() to find best finite diff step size."""

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
                self.pool = mp.Pool(n_cpus)

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
                       epsdJ=1e-4,
                       check_stability=False,
                       rtol=1e-3,
                       full_output=False):
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
        
        # diagonal entries
        def diag(i, hJ=hJ, dJ=dJ, p=p, logp2pk=self.logp2pk,
                 uix=self.coarseUix, invix=self.coarseInvix,
                 n=self.n, E=E, logZ=logZ, kStates=self.kStates):
            # round eps step to machine precision
            mxix = np.abs(dJ[i]).argmax()
            newhJ = hJ[mxix] + dJ[i][mxix]*epsdJ
            epsdJ_ = (newhJ-hJ[mxix]) / dJ[i][mxix]
            if np.isnan(epsdJ_): return 0.
            correction = calc_all_energies(n, kStates, dJ[i]*epsdJ_)
            
            # forward step
            Enew = E+correction
            modlogsumEk = logp2pk(Enew, uix, invix)
            dklplus = 2*(logsumEk - logZ - modlogsumEk + fast_logsumexp(-Enew)[0]).dot(p)
            
            # backwards step
            Enew = E-correction
            modlogsumEk = logp2pk(Enew, uix, invix)
            dklminus = 2*(logsumEk - logZ - modlogsumEk + fast_logsumexp(-Enew)[0]).dot(p)
            
            dd = (dklplus+dklminus) / np.log(2) / (2 * epsdJ_**2)
            if np.isnan(dd):
                print('nan for diag', i, epsdJ_, dklplus, dklminus)
            return dd

        def off_diag(args, hJ=hJ, dJ=dJ, p=p, logp2pk=self.logp2pk,
                     uix=self.coarseUix, invix=self.coarseInvix,
                     n=self.n, E=E, logZ=logZ, kStates=self.kStates):
            i, j = args
            
            # round eps step to machine precision
            mxix = np.abs(dJ[i]+dJ[j]).argmax()
            newhJ = hJ[mxix] + (dJ[i]+dJ[j])[mxix]*epsdJ
            epsdJ_ = (newhJ - hJ[mxix])/(dJ[i]+dJ[j])[mxix]/2
            if np.isnan(epsdJ_): return 0.
            correction = calc_all_energies(n, kStates, (dJ[i]+dJ[j])*epsdJ_)
            
            # forward step
            Enew = E+correction
            modlogsumEk = logp2pk(Enew, uix, invix)
            dklplus = (logsumEk - logZ - modlogsumEk + fast_logsumexp(-Enew)[0]).dot(p)
            
            # backwards step
            Enew = E-correction
            modlogsumEk = logp2pk(Enew, uix, invix)
            dklminus = (logsumEk - logZ - modlogsumEk + fast_logsumexp(-Enew)[0]).dot(p)

            dd = (dklplus+dklminus) / np.log(2) / (2 * epsdJ_**2)
            if np.isnan(dd):
                print('nan for off diag', args, epsdJ_, dklplus, dklminus)
            return dd
        
        hess = np.zeros((len(dJ),len(dJ)))
        if not 'pool' in self.__dict__.keys():
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

        # check for precision problems
        assert ~np.isnan(hess).any()
        assert ~np.isinf(hess).any()

        if check_stability:
            hess2 = self._maj_curvature(epsdJ=epsdJ/2, check_stability=False, hJ=hJ, dJ=dJ)
            # 4/3 ratio predicted from expansion up to 4th order term with eps/2
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

    def __get_state__(self):
        # always close multiprocess pool when pickling
        if 'pool' in self.__dict__.keys():
            self.pool.close()

        return {'n':self.n,
                'h':self.hJ[:self.n],
                'J':self.hJ[self.n:],
                'dJ':self.dJ,
                'eps':self.eps}

    def __set_state__(self, state_dict):
        self.__init__(state_dict['n'], state_dict['h'], state_dict['J'], state_dict['eps'],
                      precompute=False)
        self.dJ = state_dict['dJ']
#end IsingFisherCurvatureMethod1


class IsingFisherCurvatureMethod1a(IsingFisherCurvatureMethod1):
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
#end IsingFisherCurvatureMethod1a


class IsingFisherCurvatureMethod2(IsingFisherCurvatureMethod1):
    """Perturbation to increase symmetry between pairs of spins.
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
        solver = Enumerate(n, calc_observables_multipliers=self.ising.calc_observables)
        return (solver.solve(C)-self.hJ)/self.eps
    
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
#end IsingFisherCurvatureMethod2



class IsingFisherCurvatureMethod3(IsingFisherCurvatureMethod1):
    """Considering perturbations in both fields and couplings. Perturbations in means are
    asymmetric where ones in pairwise correlations are symmetric wrt {-1,1}.

    NOTE: This needs to be fixed up to be made compatible with latest updates to Method 1.
    """
    def compute_dJ(self, p=None, sisj=None):
        # precompute linear change to parameters for small perturbation
        dJ = np.zeros((self.n+self.n*(self.n-1), self.n+(self.n-1)*self.n//2))
        counter = 0
        for i in range(self.n):
            dJ[counter], errflag = self.solve_linearized_perturbation(i, p=p, sisj=sisj)
            counter += 1
        for i in range(self.n):
            for a in np.delete(range(self.n),i):
                dJ[counter], errflag = self.solve_linearized_perturbation(i, a, p=p, sisj=sisj)
                counter += 1
        return dJ

    def observables_after_perturbation(self, i,
                                       a=None,
                                       eps=None,
                                       perturb_up=False):
        """If a is not specified, perturb spin i's mean. Else make spin index i more like
        spin a by eps. Perturb the corresponding mean and the correlations with other
        spins j.
        
        Parameters
        ----------
        i : int
            Spin being perturbed.
        a : int, None
            Spin to mimic.
        eps : float, None
        perturb_up : bool, False

        Returns
        -------
        ndarray
            Observables <si> and <sisj> after perturbation.
        """
            
        if a is None:
            # only manipulate field
            if not hasattr(i,'__len__'):
                i = (i,)
            if not hasattr(eps,'__len__'):
                eps = eps or self.eps
                eps = [eps]*len(i)
            n = self.n
            si = self.sisj[:n]
            sisj = self.sisj[n:]

            # observables after perturbations
            siNew = si.copy()
            sisjNew = sisj.copy()
            
            if perturb_up:
                for i_,eps_ in zip(i,eps):
                    jit_observables_after_perturbation_plus_field(n, siNew, sisjNew, i_, eps_)
            else:
                for i_,eps_ in zip(i,eps):
                    jit_observables_after_perturbation_minus_field(n, siNew, sisjNew, i_, eps_)

            return np.concatenate((siNew, sisjNew))

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
        
        if perturb_up:
            for i_,a_,eps_ in zip(i,a,eps):
                jit_observables_after_perturbation_plus(n, siNew, sisjNew, i_, a_, eps_)
        else:
            for i_,a_,eps_ in zip(i,a,eps):
                jit_observables_after_perturbation_minus(n, siNew, sisjNew, i_, a_, eps_)

        return np.concatenate((siNew, sisjNew))
    
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
                                       aStar=None,
                                       p=None,
                                       sisj=None,
                                       full_output=False,
                                       eps=None,
                                       check_stability=True,
                                       disp=False):
        """Consider a perturbation to a single spin.
        NOTE: Deciding on positive or negative ssign for perturbation could help reduce
        errors.
        
        Parameters
        ----------
        iStar : int
        aStar : int, None
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
        # factor out linear dependence on eps (by default variables are perturbed down)
        dJ = np.linalg.solve(A,C)/-eps

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
#end IsingFisherCurvatureMethod3


class IsingFisherCurvatureMethod4(IsingFisherCurvatureMethod2):
    """Method2 tweaked for ternary states like C. elegans."""
    def __init__(self, n, kStates, h=None, J=None, eps=1e-7, precompute=True, n_cpus=None):
        """
        Parameters
        ----------
        n : int
        kStates : int
        h : ndarray, None
        J : ndarray, None
        eps : float, 1e-7
        precompute : bool, True
        n_cpus : int, None
        """
        import multiprocess as mp
        from coniii.utils import xpotts_states

        assert n>1 and 0<eps<.1
        self.n = n
        self.kStates = kStates
        self.eps = eps
        assert (h[:n]==0).all()
        self.hJ = np.concatenate((h,J))
        self.n_cpus = n_cpus

        self.ising = importlib.import_module('coniii.ising_eqn.ising_eqn_%d_potts'%n)
        self.sisj = self.ising.calc_observables(self.hJ)
        self.p = self.ising.p(self.hJ)
        self.allStates = np.vstack(list(xpotts_states(n, kStates))).astype(int)
        kVotes = list(map(lambda x:np.sort(np.bincount(x, minlength=3))[::-1],
                          self.allStates))
        self.coarseUix, self.coarseInvix = np.unique(kVotes, return_inverse=True, axis=0)
        
        # cache triplet and quartet products
        self._triplets_and_quartets() 
    
        if precompute:
            self.dJ = self.compute_dJ()
        else:
            self.dJ = None

    def _triplets_and_quartets(self):
        from itertools import product

        n = self.n
        self.pairs = {}
        self.triplets = {}
        self.quartets = {}
        
        # <d_{i,gammai} * d_{j,gammaj}> where i<j
        for i,j in combinations(range(n),2):
            for gammai,gammaj in product(range(self.kStates),range(self.kStates)):
                ix = (self.allStates[:,i]==gammai)&(self.allStates[:,j]==gammaj)
                self.pairs[(gammai,i,gammaj,j)] = ix

        # triplets that matter are when one spin is in a particular state and the remaining two agree with
        # each other
        for gamma in range(self.kStates):
            for i in range(n):
                for j,k in combinations(range(n),2):
                    ix = (self.allStates[:,i]==gamma)&(self.allStates[:,j]==self.allStates[:,k])
                    self.triplets[(gamma,i,j,k)] = ix
        for i,j in combinations(range(n),2):
            for k,l in combinations(range(n),2):
                ix1 = self.allStates[:,i]==self.allStates[:,j]
                ix2 = self.allStates[:,k]==self.allStates[:,l]
                self.quartets[(i,j,k,l)] = ix1&ix2

    def compute_dJ(self, p=None, sisj=None):
        # precompute linear change to parameters for small perturbation
        dJ = np.zeros((self.n*(self.n-1), 3*self.n+(self.n-1)*self.n//2))
        counter = 0
        for i in range(self.n):
            for a in np.delete(range(self.n),i):
                dJ[counter], errflag = self.solve_linearized_perturbation(i, a, p=p, sisj=sisj)
                counter += 1
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
        
        #for i_,a_,eps_ in zip(i,a,eps):
        #    jit_observables_after_perturbation_plus(n, siNew, sisjNew, i_, a_, eps_)
        perturb_up = False
        for i_,a_,eps_ in zip(i,a,eps):
            jit_observables_after_perturbation_minus(n, siNew, sisjNew, i_, a_, eps_)

        return np.concatenate((siNew, sisjNew)), perturb_up
    
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
        kStates = self.kStates
        if p is None:
            p = self.p
        if sisj is None:
            si = self.sisj[:n*kStates]
            sisj = self.sisj[kStates*n:]
        else:
            si = sisj[:kStates*n]
            sisj = sisj[kStates*n:]
        A = np.zeros((kStates*n+n*(n-1)//2, kStates*n+n*(n-1)//2))
        C = self.observables_after_perturbation(iStar, aStar, eps=eps)
        errflag = 0
        
        # mean constraints (this needs to be fixed to properly handle ternary states)
        for i in range(kStates*n):
            for k in range(kStates*n):
                if (i%n)==(k%n):
                    A[i,i] = 1 - C[i]*si[i]
                else:
                    if (i%n)<(k%n):
                        ikix = unravel_index((i%n,k%n),n)
                    else:
                        ikix = unravel_index((k%n,i%n),n)
                    A[i,k] = sisj[ikix] - C[i]*si[k]

            for klcount,(k,l) in enumerate(combinations(range(n),2)):
                A[i,kStates*n+klcount] = self.triplets[(i%n,k%n,l)].dot(p) - C[i]*sisj[klcount]
        
        # pair constraints (needs to be fixed to handle ternary means)
        for ijcount,(i,j) in enumerate(combinations(range(n),2)):
            for k in range(n):
                A[kStates*n+ijcount,k] = self.triplets[(i,j,k)].dot(p) - C[kStates*n+ijcount]*si[k]
            for klcount,(k,l) in enumerate(combinations(range(n),2)):
                A[kStates*n+ijcount,kStates*n+klcount] = (self.quartets[(i,j,k,l)].dot(p) -
                                                          C[kStates*n+ijcount]*sisj[klcount])
    
        C -= self.sisj
        # factor out linear dependence on eps
        dJ = np.linalg.solve(A,C)/-eps

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
#end IsingFisherCurvatureMethod4



class IsingFisherCurvatureMethod4a(IsingFisherCurvatureMethod4):
    """Method4 (ternary states) with mean perturbations."""
    def compute_dJ(self, p=None, sisj=None):
        # precompute linear change to parameters for small perturbation
        dJ = np.zeros((self.kStates*self.n, self.kStates*self.n+(self.n-1)*self.n//2))
        counter = 0
        for k in range(self.kStates):
            for i in range(self.n):
                dJ[counter], errflag = self.solve_linearized_perturbation(i, k, p=p, sisj=sisj)
                counter += 1
        return dJ
        
    def _observables_after_perturbation(self, si, sisj, i, gamma, eps):
        """Make component i point in k state by eps more.

        Parameters
        ----------
        si : ndarray
            Mean probability of being in each k state.
        sisj : ndarray
            Pairwise correlations.
        i : int
            Spin index.
        gamma : int
            Objective state.
        eps : float
            Perturbation.
        """
        
        n = self.n
        siCopy = si.copy()
        sisjCopy = sisj.copy()

        if gamma==0:
            si[i] = siCopy[i] + eps*(siCopy[n+i]+siCopy[2*n+i])
            si[n+i] = (1-eps)*siCopy[n+i]
            si[2*n+i] = (1-eps)*siCopy[2*n+i]
        elif gamma==1:
            si[n+i] = siCopy[n+i] + eps*(siCopy[i]+siCopy[2*n+i])
            si[i] = (1-eps)*siCopy[i]
            si[2*n+i] = (1-eps)*siCopy[2*n+i]
        elif gamma==2:
            si[2*n+i] = siCopy[2*n+i] + eps*(siCopy[i]+siCopy[n+i])
            si[i] = (1-eps)*siCopy[i]
            si[n+i] = (1-eps)*siCopy[n+i]

        for j in range(n):
            if i!=j:
                if i<j:
                    ijix = unravel_index((i, j), n)
                    sisj[ijix] = self.pairs[(gamma,i,gamma,j)].dot(self.p)
                    
                    for k in range(self.kStates):
                        if k!=gamma:
                            sisj[ijix] += eps*self.pairs[(k,i,gamma,j)].dot(self.p)
                            sisj[ijix] += (1-eps)*self.pairs[(k,i,k,j)].dot(self.p)

                elif j<i:
                    ijix = unravel_index((j, i), n)
                    sisj[ijix] = self.pairs[(gamma,j,gamma,i)].dot(self.p)

                    for k in range(self.kStates):
                        if k!=gamma:
                            sisj[ijix] += eps*self.pairs[(gamma,j,k,i)].dot(self.p)
                            sisj[ijix] += (1-eps)*self.pairs[(k,j,k,i)].dot(self.p)

    def observables_after_perturbation(self, i, k, eps=None):
        """Make spin index i more like spin a by eps. Perturb the corresponding mean and
        the correlations with other spins j.
        
        Parameters
        ----------
        i : int
            Spin being perturbed.
        k : int
            Index of state type.
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
        if not hasattr(eps,'__len__'):
            eps = eps or self.eps
            eps = [eps]*len(i)
        n = self.n
        si = self.sisj[:n*self.kStates]
        sisj = self.sisj[self.kStates*n:]

        # observables after perturbations
        siNew = si.copy()
        sisjNew = sisj.copy()
        for i_,eps_ in zip(i,eps):
            self._observables_after_perturbation(siNew, sisjNew, i_, k, eps_)

        return np.concatenate((siNew, sisjNew)), True
    
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

    def _solve_linearized_perturbation_tester(self, iStar, gamma):
        """
        ***FOR DEBUGGING ONLY***
        """
        
        n = self.n
        k = self.kStates
        p = self.p
        C = self.observables_after_perturbation(iStar, gamma, eps=self.eps)[0]

        from coniii.solvers import Enumerate
        solver = Enumerate(n, calc_observables_multipliers=self.ising.calc_observables)
        soln = solver.solve(C, initial_guess=self.hJ, scipy_solver_kwargs={'method':'hybr'})
        soln[:n*k] -= np.tile(soln[:n], k)
        return (soln - self.hJ)/(self.eps)
#end IsingFisherCurvatureMethod4a


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
    si[i] = si[i] + eps

    for j in delete(list(range(n)),i):
        if i<j:
            ijix = unravel_index((i,j),n)
        else:
            ijix = unravel_index((j,i),n)

        sisj[ijix] = sisj[ijix] + eps*si[j]

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
