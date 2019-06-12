# =============================================================================================== #
# Test module for Median Voter Model.
# Author: Eddie Lee, edl56@cornell.edu
# =============================================================================================== #
from .mvm import *
from importlib import import_module


def test_corr():
    for n in [5,7,9,11]:
        assert np.isclose(pair_corr(bin_states(n,True), weights=create_mvm_p(n, 1))[1][0],
                          corr(n)[0])

def test_couplings():
    np.random.seed(0)
    J = np.random.normal(size=4, scale=.5)

    for n in [5,7,9,11]:
        smo_fun, smop_fun, soo_fun, soop_fun, _ = setup_maxent(n)
        Jmo, Joo = couplings(n)
        assert np.isclose([smo_fun(Jmo,Jmo,Joo,Joo), soo_fun(Jmo,Jmo,Joo,Joo)],
                          np.array(corr(n)), atol=1e-7).all()
        print("Test passed: numerically solved couplings return expected correlations for n=%d."%n)

        Js, soln = couplings(n, data_corr=(smo_fun(*J), smop_fun(*J), soo_fun(*J), soop_fun(*J)),
                             full_output=True)
        # couplings do not have to be so accurate to match correlations
        # but numerical precision becomes a noticeable issue even for n=11
        #assert np.isclose(J, Js, atol=1e-2).all(), (np.linalg.norm(J - Js), J, Js, soln['message'])
        corr1 = np.array([smo_fun(*J), smop_fun(*J), soo_fun(*J), soop_fun(*J)])
        corr2 = np.array([smo_fun(*Js), smop_fun(*Js), soo_fun(*Js), soop_fun(*Js)])
        corrErr = np.linalg.norm(corr1-corr2)
        assert corrErr<1e-6, corrErr
        print("Test passed: original couplings returned for n=%d."%n)

def test_setup_maxent():
    np.random.seed(0)
    Jmo, Jmop, Joo, Jop = np.random.normal(size=4, scale=.3)
    nRange = [5,7,9,11]

    for i,n in enumerate(nRange):
        ising = import_module('coniii.ising_eqn.ising_eqn_%d_sym'%n)
        hJ = np.zeros(n+n*(n-1)//2)
        hJ[n:2*n-1] = Jmo
        hJ[n] = Jmop
        hJ[2*n-1:] = Joo
        hJ[2*n-1:2*n-1+n-2] = Jop
        sisjME = ising.calc_observables(hJ)
        # extract corresponding pairwise correlations from full pairwise maxent model
        smoME, smopME, sooME, sopME = sisjME[n+1], sisjME[n], sisjME[-1], sisjME[n+n-1]
         
        smo, smop, soo, sop, pk = setup_maxent(n)
        assert np.isclose( smoME, smo(Jmo, Jmop, Joo, Jop) )
        assert np.isclose( smopME, smop(Jmo, Jmop, Joo, Jop) )
        
        assert np.isclose( sooME, soo(Jmo, Jmop, Joo, Jop) )
        assert np.isclose( sopME, sop(Jmo, Jmop, Joo, Jop) )

        p = ising.p(hJ)
        k = bin_states(n).sum(1)
        k[k<n/2] = n - k[k<n/2]
        pkME = np.array([p[k==i].sum() for i in range(n//2+1,n+1)])
        assert np.isclose( pkME, pk(Jmo, Jmop, Joo, Jop) ).all()
    print("Test passed: Pairwise correlations agree with ConIII module.")