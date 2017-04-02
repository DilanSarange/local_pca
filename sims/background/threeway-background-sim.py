#!/usr/bin/env python3.5
description = '''
Simulate with simuPOP AND write to msprime/vcf:
    a population arrangement switches from A<-B|C to A|B->C 
    at some point in the past.
'''

import gzip
import sys
from optparse import OptionParser
import math
import time
import random
from ftprime import RecombCollector, ind_to_chrom, mapa_labels
import msprime
import argparse

def fileopt(fname,opts):
    '''Return the file referred to by fname, open with options opts;
    if fname is "-" return stdin/stdout; if fname ends with .gz run it through gzip.
    '''
    if fname == "-":
        if opts == "r":
            fobj = sys.stdin
        elif opts == "w":
            fobj = sys.stdout
        else:
            print("Something not right here.")
    elif fname[len(fname)-3:len(fname)]==".gz":
        fobj = gzip.open(fname,opts)
    else:
        fobj = open(fname,opts)
    return fobj

parser = argparse.ArgumentParser(description=description)
parser.add_argument('--relative_switch_time', '-w', default=0.25, type=float, 
        help="Time since rearrangement of populations in units of population size.")
parser.add_argument('--relative_fast_M', '-m', default=10, type=float, 
        help="Migration rate for 'close' pops in units of population size.")
parser.add_argument('--relative_slow_m', '-M', default=0.1, type=float, 
        help="Migration rate for 'distant' pops in units of population size.")
parser.add_argument("--generations", "-T", type=int, help="number of generations to run for before the switch")
parser.add_argument("--popsize", "-N", type=int, help="size of each subpopulation", default=100)
parser.add_argument("--length", "-L", type=float, help="number of bp in the chromosome", default=1e4)
parser.add_argument("--nloci", "-l", type=int, help="number of selected loci", default=20)
parser.add_argument("--sel_mut_rate", "-u", type=float, help="mutation rate of selected alleles", default=1e-7)
parser.add_argument("--recomb_rate", "-r", type=float, help="recombination rate", default=1e-7)
parser.add_argument("--gamma_alpha", "-a", type=float, help="alpha parameter in gamma distributed selection coefficients", default=.23)
parser.add_argument("--gamma_beta", "-b", type=float, help="beta parameter in gamma distributed selection coefficients", default=5.34)
parser.add_argument("--nsamples", "-k", type=int, help="number of *diploid* samples, total")
parser.add_argument("--ancestor_age", "-A", type=float, help="time to ancestor above beginning of sim")
parser.add_argument("--mut_rate", "-U", type=float, help="mutation rate", default=1e-7)

parser.add_argument("--treefile", "-t", help="name of output file for trees (default: not output)", default=None)
parser.add_argument("--outfile", "-o", help="name of output VCF file (default: not output)", default=None)
parser.add_argument("--logfile", "-g", help="name of log file (or '-' for stdout)", default="-")
parser.add_argument("--selloci_file", "-s", help="name of file to output selected locus information", default="sel_loci.txt")

args = parser.parse_args()

import simuOpt
simuOpt.setOptions(alleleType='mutant')
import simuPOP as sim
from simuPOP.demography import migr2DSteppingStoneRates, migrSteppingStoneRates

if args.outfile is not None:
    outfile = fileopt(args.outfile, "w")
logfile = fileopt(args.logfile, "w")
selloci_file = args.selloci_file

logfile.write("Options:\n")
logfile.write(str(args)+"\n")
logfile.write(time.strftime('%X %x %Z')+"\n")
logfile.write("----------\n")
logfile.flush()

npops=3

# increase spacing between loci as we go along the chromosome
rel_positions=[0.0 for k in range(args.nloci)]
for k in range(args.nloci):
    rel_positions[k] = rel_positions[k-1] + random.expovariate(1)*(k**2)
pos_fac=args.length/(rel_positions[-1]+random.expovariate(1)*(args.nloci**2))
locus_position=[x*pos_fac for x in rel_positions]

# initially polymorphic alleles
init_freqs=[[k/100,1-k/100,0,0] for k in range(1,11)]
locus_classes=[min(len(init_freqs)-1,math.floor(random.expovariate(1))) for k in range(args.nloci)]
init_classes=[list(filter(lambda k: locus_classes[k]==x,range(args.nloci))) for x in range(len(init_freqs))]

logfile.write("Locus positions:\n")
logfile.write(str(locus_position)+"\n")
logfile.write("----------\n")
logfile.flush()

init_geno=[sim.InitGenotype(freq=init_freqs[k],loci=init_classes[k]) for k in range(len(init_freqs))]

###
# modified from http://simupop.sourceforge.net/manual_svn/build/userGuide_ch5_sec9.html

class GammaDistributedFitness:
    def __init__(self, alpha, beta):
        # mean is alpha/beta
        self.coefMap = {}
        self.alpha = alpha
        self.beta = beta
    def __call__(self, loc, alleles):
        # because s is assigned for each locus, we need to make sure the
        # same s is used for fitness of genotypes 01 (1-s) and 11 (1-2s)
        # at each locus
        if loc in self.coefMap:
            s = self.coefMap[loc]
        else:
            s = random.gammavariate(self.alpha, self.beta)
            self.coefMap[loc] = s
        # print(str(loc)+":"+str(alleles)+"\n")
        # needn't return fitness for alleles=(0,0) as simupop knows that's 1
        if 0 in alleles:
            return 1. - s
        else:
            return 1. - 2.*s

pop = sim.Population(
        size=[args.popsize]*npops, 
        loci=[args.nloci], 
        lociPos=locus_position,
        infoFields=['ind_id','fitness','migrate_to'])

args.fast_M = args.relative_fast_M/(4*args.popsize)
args.slow_m = args.relative_slow_m/(4*args.popsize)
args.switch_time = int(args.relative_switch_time*(4*args.popsize))

migr_init = [ [ 0, args.slow_m, args.slow_m ],
              [ args.fast_M, 0, args.slow_m ],
              [ args.slow_m, args.slow_m, 0 ]]

migr_change = [ [ 0, args.slow_m, args.slow_m ],
                [ args.slow_m, 0, args.fast_M ],
                [ args.slow_m, args.slow_m, 0 ]]

# record recombinations
rc = RecombCollector(
        nsamples=args.nsamples, generations=args.generations+args.switch_time, 
        N=args.popsize*npops,
        ancestor_age=args.ancestor_age, length=args.length, locus_position=locus_position)

pop.evolve(
    initOps=[
        sim.InitSex(),
        sim.IdTagger(),
    ]+init_geno,
    preOps=[
        sim.Migrator(
            rate=migr_init,
            mode=sim.BY_PROBABILITY,
            begin=0, end=args.switch_time),
        sim.Migrator(
            rate=migr_change,
            mode=sim.BY_PROBABILITY,
            begin=args.switch_time),
        sim.AcgtMutator(rate=[args.sel_mut_rate], model='JC69'),
        sim.PyMlSelector(GammaDistributedFitness(args.gamma_alpha, args.gamma_beta),
            output=">>"+selloci_file),
    ],
    matingScheme=sim.RandomMating(
        ops=[
            sim.IdTagger(),
            sim.Recombinator(intensity=args.recomb_rate,
                output=rc.collect_recombs,
                infoFields="ind_id"),
        ] ),
    postOps=[
        sim.Stat(numOfSegSites=sim.ALL_AVAIL, step=50),
        sim.PyEval(r"'Gen: %2d #seg sites: %d\n' % (gen, numOfSegSites)", step=50)
    ],
    gen = args.generations
)

logfile.write("Done simulating!\n")
logfile.write(time.strftime('%X %x %Z')+"\n")
logfile.write("----------\n")
logfile.flush()

# writes out events in this form:
# offspringID parentID startingPloidy rec1 rec2 ....

rc.add_samples(pop.indInfo("ind_id"))

logfile.write("Samples:\n")
logfile.write(str(rc.diploid_samples)+"\n")
logfile.write("----------\n")
logfile.flush()

# not really the sample locs, but we don't care about that
sample_locs = [ (0,0) for _ in range(rc.nsamples) ]

ts = rc.args.tree_sequence(samples=sample_locs)

logfile.write("Loaded into tree sequence!\n")
logfile.write(time.strftime('%X %x %Z')+"\n")
logfile.write("----------\n")
logfile.flush()

minimal_ts = ts.simplify()
del ts

logfile.write("Simplified; now writing to treefile (if specified).\n")
logfile.write(time.strftime('%X %x %Z')+"\n")
logfile.write("----------\n")
logfile.flush()

mut_seed=random.randrange(1,1000)
logfile.write("Generating mutations with seed "+str(mut_seed)+"\n")
rng = msprime.RandomGenerator(mut_seed)
minimal_ts.generate_mutations(args.mut_rate,rng)

if args.treefile is not None:
    minimal_ts.dump(args.treefile)

logfile.write("Generated mutations!\n")
logfile.write(time.strftime('%X %x %Z')+"\n")
logfile.write("Mean pairwise diversity: {}\n".format(minimal_ts.get_pairwise_diversity()/minimal_ts.get_sequence_length()))
logfile.write("Sequence length: {}\n".format(minimal_ts.get_sequence_length()))
logfile.write("Number of trees: {}\n".format(minimal_ts.get_num_trees()))
logfile.write("Number of mutations: {}\n".format(minimal_ts.get_num_mutations()))

if args.outfile is None:
    print("NOT writing out genotype data.\n")
else:
    minimal_ts.write_vcf(outfile,ploidy=1)


logfile.write("All done!\n")
logfile.close()
