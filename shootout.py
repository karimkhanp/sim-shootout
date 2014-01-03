#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2013 Radim Rehurek <me@radimrehurek.com>

"""
USAGE: %(program)s INPUT_DIRECTORY

Compare speed and accuracy of several similarity retrieval methods, using the corpus prepared by prepare_shootout.py.

Example: ./shootout.py ~/data/wiki/shootout

"""

import os
import sys
import time
import logging
import itertools
from functools import wraps

import numpy

import gensim

MAX_DOCS = 10000000  # clip the dataset at this many docs, if larger (=use a wiki subset)
TOP_N = 50  # how many similars to ask for
ACC = 'high'  # what accuracy are we aiming for (avg k-NN diff = cumulative gain); tuned so that low=0.1, high=0.01 at k=50
NUM_QUERIES = 100  # query with this many different, randomly selected documents
REPEATS = 3  # run all queries this many times, take the best timing

ACC_SETTINGS = {
    'flann': {'low': 0.7, 'high': 0.95},
    'annoy': {'low': 12, 'high': 100},
    'lsh': {'low': {'k': 10, 'l': 10}, 'high': {'k': 10, 'l': 10}},
}


logger = logging.getLogger('shootout')

def profile(fn):
    @wraps(fn)
    def with_profiling(*args, **kwargs):
        times = []
        logger.info("benchmarking %s at k=%s acc=%s" % (fn.__name__, TOP_N, ACC))
        for _ in xrange(REPEATS):  # try running it three times, report the best time
            start = time.time()
            ret = fn(*args, **kwargs)
            times.append(time.time() - start)
        logger.info("%s took %.3fms/query" % (fn.__name__, 1000.0 * min(times) / NUM_QUERIES))
        logger.info("%s raw timings: %s" % (fn.__name__, times))
        return ret

    return with_profiling


@profile
def gensim_1by1(index, queries):
    for query in queries:
        _ = index[query]

@profile
def gensim_at_once(index, queries):
    _ = index[queries]

@profile
def flann_1by1(index, queries):
    for query in queries:
        _ = index.nn_index(query, TOP_N)

@profile
def flann_at_once(index, queries):
    _ = index.nn_index(queries, TOP_N)

@profile
def sklearn_1by1(index, queries):
    for query in queries:
        _ = index.kneighbors(query, n_neighbors=TOP_N)

@profile
def sklearn_at_once(index, queries):
    _ = index.kneighbors(queries, n_neighbors=TOP_N)

@profile
def annoy_1by1(index, queries):
    for query in queries:
        _ = index.get_nns_by_vector(list(query.astype(float)), TOP_N)

@profile
def lsh_1by1(index, queries):
    for query in queries:
        _ = index.Find(query[:, None])[:TOP_N]


def flann_predictions(index, queries):
    if TOP_N == 1:
        # flann returns differently shaped arrays when asked for only 1 nearest neighbour
        return [index.nn_index(query, TOP_N)[0] for query in queries]
    else:
        return [index.nn_index(query, TOP_N)[0][0] for query in queries]


def sklearn_predictions(index, queries):
    return [list(index.kneighbors(query, TOP_N)[1].ravel()) for query in queries]


def annoy_predictions(index, queries):
    return [index.get_nns_by_vector(list(query.astype(float)), TOP_N) for query in queries]


def lsh_predictions(index, queries):
    return [[pos for pos, _ in index_lsh.Find(query[:, None])[:TOP_N]] for query in queries]


def gensim_predictions(index, queries):
    return [[pos for pos, _ in index[query]] for query in queries]


def get_accuracy(predicted_ids, queries, gensim_index, expecteds=None):
    """Return precision (=percentage of overlapping ids) and average similarity difference."""
    logger.info("computing ground truth")
    correct, diffs = 0.0, []
    gensim_index.num_best = TOP_N
    for query_no, (predicted, query) in enumerate(zip(predicted_ids, queries)):
        expected_ids, expected_sims = zip(*gensim_index[query]) if expecteds is None else expecteds[query_no]
        correct += len(set(expected_ids).intersection(predicted))
        predicted_sims = [numpy.dot(gensim_index.vector_by_id(id1), query) for id1 in predicted]
        # if we got less than TOP_N results, assume zero similarity for the missing ids (LSH)
        predicted_sims.extend([0.0] * (TOP_N - len(predicted_sims)))
        diffs.extend(-numpy.array(predicted_sims) + expected_sims)
    return correct / (TOP_N * len(queries)), 1.0 * sum(diffs) / len(diffs)


def log_precision(method, index, queries, gensim_index, expecteds=None):
    logger.info("computing accuracy of %s at k=%s, acc=%s" % (method.__name__, TOP_N, ACC))
    acc, diffs = get_accuracy(method(index, queries), queries, gensim_index, expecteds)
    logger.info("%s precision=%.3f, avg diff=%.3f" % (method.__name__, acc, diffs))


def print_similar(title, index_gensim, id2title, title2id):
    """Print out the most similar Wikipedia articles, given an article title=query"""
    pos = title2id[title.lower()]  # throws if title not found
    for pos2, sim in index_gensim[index_gensim.vector_by_id(pos)]:
        print pos2, `id2title[pos2]`, sim


if __name__ == '__main__':
    logging.basicConfig(format='%(asctime)s : %(levelname)s : %(message)s')
    logging.root.setLevel(level=logging.INFO)
    logger.info("running %s" % ' '.join(sys.argv))

    # check and process input arguments
    program = os.path.basename(sys.argv[0])
    if len(sys.argv) < 2:
        print globals()['__doc__'] % locals()
        sys.exit(1)
    indir = sys.argv[1]
    if len(sys.argv) > 2:
        TOP_N = int(sys.argv[2])
    if len(sys.argv) > 3:
        ACC = sys.argv[3]
    lsi_vectors = os.path.join(indir, 'lsi_vectors.mm.gz')
    logger.info("testing k=%s and avg diff=%s" % (TOP_N, ACC))

    mm = gensim.corpora.MmCorpus(gensim.utils.smart_open(lsi_vectors))
    num_features, num_docs = mm.num_terms, min(mm.num_docs, MAX_DOCS)
    sim_prefix = os.path.join(indir, 'index%s' % num_docs)

    # some libs (flann, sklearn) expect the entire input as a full matrix, all at once (no streaming)
    if os.path.exists(sim_prefix + "_clipped.npy"):
        logger.info("loading dense corpus (need for flann, scikit-learn)")
        clipped = numpy.load(sim_prefix + "_clipped.npy", mmap_mode='r')
    else:
        logger.info("creating dense corpus of %i documents under %s" % (num_docs, sim_prefix + "_clipped.npy"))
        clipped = numpy.empty((num_docs, num_features), dtype=numpy.float32)
        for docno, doc in enumerate(itertools.islice(mm, num_docs)):
            if docno % 100000 == 0:
                logger.info("at document #%i/%i" % (docno + 1, num_docs))
            clipped[docno] = gensim.matutils.sparse2full(doc, num_features)
        numpy.save(sim_prefix + "_clipped.npy", clipped)
    clipped_corpus = gensim.matutils.Dense2Corpus(clipped, documents_columns=False)  # same as islice(mm, num_docs)

    logger.info("selecting %s documents, to act as top-%s queries" % (NUM_QUERIES, TOP_N))
    queries = clipped[:NUM_QUERIES]

    if os.path.exists(sim_prefix + "_gensim"):
        logger.info("loading gensim index")
        index_gensim = gensim.similarities.Similarity.load(sim_prefix + "_gensim")
        index_gensim.output_prefix = sim_prefix
        index_gensim.check_moved()  # update shard locations in case the files were copied somewhere else
    else:
        logger.info("building gensim index")
        index_gensim = gensim.similarities.Similarity(sim_prefix, clipped_corpus, num_best=TOP_N, num_features=num_features, shardsize=100000)
        index_gensim.save(sim_prefix + "_gensim")
    logger.info("finished gensim index %s" % index_gensim)

    logger.info("loading mapping between article titles and ids")
    id2title = gensim.utils.unpickle(os.path.join(indir, 'id2title'))
    title2id = dict((title.lower(), pos) for pos, title in enumerate(id2title))
    # print_similar('Anarchism', index_gensim, id2title, title2id)

    if 'gensim' in program:
        # log_precision(gensim_predictions, index_gensim, queries, index_gensim)  FIXME
        gensim_at_once(index_gensim, queries)
        gensim_1by1(index_gensim, queries)

    if 'flann' in program:
        import pyflann
        pyflann.set_distance_type('euclidean')
        index_flann = pyflann.FLANN()
        flann_fname = sim_prefix + "_flann_%s" % ACC
        if os.path.exists(flann_fname):
            logger.info("loading flann index")
            index_flann.load_index(flann_fname, clipped)
        else:
            logger.info("building FLANN index")
            # flann expects index vectors as a 2d numpy array, features = columns
            params = index_flann.build_index(clipped, algorithm="autotuned", target_precision=ACC_SETTINGS['flann'][ACC], log_level="info")
            logger.info("built flann index with %s" % params)
            index_flann.save_index(flann_fname)
        logger.info("finished FLANN index")

        log_precision(flann_predictions, index_flann, queries, index_gensim)
        flann_1by1(index_flann, queries)
        flann_at_once(index_flann, queries)

    if 'annoy' in program:
        import annoy
        index_annoy = annoy.AnnoyIndex(num_features, metric='angular')
        annoy_fname = sim_prefix + "_annoy_%s" % ACC
        if os.path.exists(annoy_fname):
            logger.info("loading annoy index")
            index_annoy.load(annoy_fname)
        else:
            logger.info("building annoy index")
            # annoy expects index vectors as lists of Python floats
            for i, vec in enumerate(clipped_corpus):
                index_annoy.add_item(i, list(gensim.matutils.sparse2full(vec, num_features).astype(float)))
            index_annoy.build(ACC_SETTINGS['annoy'][ACC])
            index_annoy.save(annoy_fname)
            logger.info("built annoy index")

        log_precision(annoy_predictions, index_annoy, queries, index_gensim)
        annoy_1by1(index_annoy, queries)

    if 'lsh' in program:
        import lsh
        if os.path.exists(sim_prefix + "_lsh"):
            logger.info("loading lsh index")
            index_lsh = gensim.utils.unpickle(sim_prefix + "_lsh")
        else:
            logger.info("building lsh index")
            index_lsh = lsh.index(w=float('inf'), k=ACC_SETTINGS['lsh'][ACC]['k'], l=ACC_SETTINGS['lsh'][ACC]['l'])
            # lsh expects input as D x 1 numpy arrays
            for vecno, vec in enumerate(clipped_corpus):
                index_lsh.InsertIntoTable(vecno, gensim.matutils.sparse2full(vec)[:, None])
            gensim.utils.pickle(index_lsh, sim_prefix + '_lsh')
        logger.info("finished lsh index")

        log_precision(lsh_predictions, index_lsh, queries, index_gensim)
        lsh_1by1(index_lsh, queries)

    if 'sklearn' in program:
        from sklearn.neighbors import NearestNeighbors
        if os.path.exists(sim_prefix + "_sklearn"):
            logger.info("loading sklearn index")
            index_sklearn = gensim.utils.unpickle(sim_prefix + "_sklearn")
        else:
            logger.info("building sklearn index")
            index_sklearn = NearestNeighbors(n_neighbors=TOP_N, algorithm='auto').fit(clipped)
            logger.info("built sklearn index %s" % index_sklearn._fit_method)
            gensim.utils.pickle(index_sklearn, sim_prefix + '_sklearn')
        logger.info("finished sklearn index")

        log_precision(sklearn_predictions, index_sklearn, queries, index_gensim)
        sklearn_1by1(index_sklearn, queries)
        sklearn_at_once(index_sklearn, queries)

    logger.info("finished running %s" % program)
