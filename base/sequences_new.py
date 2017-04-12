'''
parse, subsample, and align a sequence data set
'''
from __future__ import division, print_function
import os, re, time, csv, sys
from io_util import myopen
# from io_util import myopen, make_dir, remove_dir, tree_to_json, write_json
from collections import defaultdict
from Bio import SeqIO
import numpy as np
from seq_util import pad_nucleotide_sequences, nuc_alpha, aa_alpha
from datetime import datetime
import json

TINY = 1e-10

def fix_names(n):
    return n.replace(" ","_").replace("(",'_').replace(")",'_').replace("'",'_').replace(":",'_')
#
# def calc_af(aln, alpha):
#     aln_array = np.array(aln)
#     af = np.zeros((len(alpha), aln_array.shape[1]))
#     for ai, state in enumerate(alpha):
#         af[ai] += (aln_array==state).mean(axis=0)
#     af[-1] = 1.0 - af[:-1].sum(axis=0)
#     return af

def num_date(date):
    days_in_year = date.toordinal()- datetime(year=date.year, month=1, day=1).date().toordinal()
    return date.year + days_in_year/365.25

def ambiguous_date_to_date_range(mydate, fmt):
    sep = fmt.split('%')[1][-1]
    min_date, max_date = {}, {}
    today = datetime.today().date()

    for val, field  in zip(mydate.split(sep), fmt.split(sep+'%')):
        f = 'year' if 'y' in field.lower() else ('day' if 'd' in field.lower() else 'month')
        if 'XX' in val:
            if f=='year':
                return None, None
            elif f=='month':
                min_date[f]=1
                max_date[f]=12
            elif f=='day':
                min_date[f]=1
                max_date[f]=31
        else:
            min_date[f]=int(val)
            max_date[f]=int(val)
    max_date['day'] = min(max_date['day'], 31 if max_date['month'] in [1,3,5,7,8,10,12]
                                           else 28 if max_date['month']==2 else 30)
    lower_bound = datetime(year=min_date['year'], month=min_date['month'], day=min_date['day']).date()
    upper_bound = datetime(year=max_date['year'], month=max_date['month'], day=max_date['day']).date()
    return (lower_bound, upper_bound if upper_bound<today else today)

class sequence_set(object):
    """ sequence set deals with loading sequences (stored in self.seqs)
    and various basic tasks including filtering, output etc """

    def __init__(self, logger, segmentName):
        super(sequence_set, self).__init__()
        self.log = logger
        self.segmentName = segmentName
        self.extras = {}

    def load_mfa(self, path):
        try:
            with myopen(path) as seq_file:
                self.seqs = {x.name:x for x in SeqIO.parse(seq_file, 'fasta')}
        except Exception as e:
            self.log.fatal("Error loading sequences from {}. Error: {}".format(path, e))
        self.nstart = len(self.seqs)
        self.log.notify("Loaded {} sequences from {}".format(self.nstart, path))

    def ungap(self):
        '''
        remove previously existing gaps and make sure all sequences are upper case
        '''
        for seq in self.seqs.values():
            seq.seq = seq.seq.ungap('-').upper()

    def parse_headers(self, fields, sep='|', strip='_'):
        '''
        split the sequence description and add annotations to sequences
        '''
        try:
            assert("strain" in fields.values())
        except AssertionError:
            self.log.fatal("Config file: fasta_fields must contain 'strain'")
        for seq in self.seqs.values():
            if not hasattr(seq, "attributes"): seq.attributes = {}
            words = map(lambda x:fix_names(x), seq.description.replace(">","").split(sep))
            for ii, val in enumerate(words):
                if ii in fields:
                    if val not in ["", "-"]:
                        # self.log.debug("{} -> {}".format(fields[ii], val))
                        seq.attributes[fields[ii]] = val
                    else:
                        seq.attributes[fields[ii]] = ""
        self.seqs = {seq.attributes['strain']:seq for seq in self.seqs.values()}
        for seq in self.seqs.values():
            seq.id = seq.attributes['strain']
            seq.name = seq.attributes['strain']

    def load_json(self, path):
        pass

    def parse_date(self, fmts, prune):
        if not hasattr(self.seqs.values()[0], "attributes"):
            self.log.fatal("parse meta info first")
        from datetime import datetime
        for seq in self.seqs.values():
            if 'date' in seq.attributes and seq.attributes['date']!='':
                for fmt in fmts:
                    try:
                        if 'XX' in seq.attributes['date']:
                            min_date, max_date = ambiguous_date_to_date_range(seq.attributes['date'], fmt)
                            seq.attributes['raw_date'] = seq.attributes['date']
                            seq.attributes['num_date'] = np.array((num_date(min_date), num_date(max_date)))
                            seq.attributes['date'] = min_date
                        else:
                            if callable(fmt):
                                tmp = fmt(seq.attributes['date'])
                            else:
                                try:
                                    tmp = datetime.strptime(seq.attributes['date'], fmt).date()
                                except:
                                    tmp = seq.attributes['date']
                            seq.attributes['raw_date'] = seq.attributes['date']
                            seq.attributes['num_date'] = num_date(tmp)
                            seq.attributes['date']=tmp
                            break
                    except:
                        continue

        # helpful debugging statements:
        # for seq in self.seqs.values():
        #     try:
        #         self.log.debug("{} date: {}, raw_date: {}, num_date: {}".format(seq.name, seq.attributes['date'], seq.attributes['raw_date'], seq.attributes['num_date']))
        #     except KeyError:
        #         self.log.debug("{} missing date(s)".format(seq.name))

        if prune:
            self.filterSeqs("Missing Date", lambda x:'date' in x.attributes and type(x.attributes['date'])!=str)

    def filterSeqs(self, funcName, func):
        names = set(self.seqs.keys())
        self.seqs = {key:seq for key, seq in self.seqs.iteritems() if func(seq)} #or key==self.reference_seq.name
        for name in names - set(self.seqs.keys()):
            self.log.drop(name, self.segmentName, funcName)

    # def filter(self, func, leave_ref=False):
    #     if leave_ref:
    #         self.all_seqs = {key:seq for key, seq in self.all_seqs.iteritems() if func(seq) or key==self.reference_seq.name}
    #     else:
    #         self.all_seqs = {key:seq for key, seq in self.all_seqs.iteritems() if func(seq)}
    #     print("Filtered to %d sequences"%len(self.all_seqs))

    def subsample(self, config):
        '''
        produce a useful set of sequences from the raw input.
        arguments:
        category  -- callable that assigns each sequence to a category for subsampling
        priority  -- callable that assigns each sequence a priority to be included in
                     the final sample. this is applied independently in each category
        threshold -- callable that determines the number of sequences from each category
                     that is included in the final set. takes arguments, cat and seq
                     alternatively can be an int
        '''
        # default filters:
        category = lambda x: (x.attributes['date'].year, x.attributes['date'].month)
        priority = lambda x: np.random.random()
        threshold = lambda x: 5
        # try load them from config
        try:
            if callable(config["subsample"]["category"]):
                category = config["subsample"]["category"]
        except KeyError:
            pass
        try:
            if callable(config["subsample"]["priority"]):
                priority = config["subsample"]["priority"]
        except KeyError:
            pass
        try:
            if callable(config["subsample"]["threshold"]):
                threshold = config["subsample"]["threshold"]
            elif type(config["subsample"]["threshold"]) is int:
                threshold = lambda x: config["subsample"]["threshold"]
        except KeyError:
            pass

        self.sequence_categories = defaultdict(list)
        names_prior = set(self.seqs.keys())
        seqs_to_subsample = self.seqs.values()

        # sort sequences into categories and assign priority score
        for seq in seqs_to_subsample:
            seq._priority = priority(seq)
            self.sequence_categories[category(seq)].append(seq)

        # sample and record the degree to which a category is under_sampled
        self.seqs = {}
        for cat, seqs in self.sequence_categories.iteritems():
            under_sampling = min(1.00, 1.0*len(seqs)/threshold(cat))
            for s in seqs: s.under_sampling=under_sampling
            seqs.sort(key=lambda x:x._priority, reverse=True)
            self.seqs.update({seq.id:seq for seq in seqs[:threshold(cat)]})

        self.log.notify("Subsampling segment {}. n={} -> {}".format(self.segmentName, len(seqs_to_subsample), len(self.seqs)))
        for name in names_prior - set(self.seqs.keys()):
            self.log.drop(name, self.segmentName, "subsampled")


    # def strip_non_reference(self):
    #     ungapped = np.array(self.sequence_lookup[self.reference_seq.name])!='-'
    #     from Bio.Seq import Seq
    #     for seq in self.aln:
    #         seq.seq = Seq("".join(np.array(seq)[ungapped]))

    def get_trait_values(self, trait):
        vals = set()
        for seq, obj in self.seqs.iteritems():
            if trait in obj.attributes:
                vals.add(obj.attributes[trait])
        # don't forget the reference here


        return vals

    def write_json(self, fh, config):
        # datetime() objects and [arrays] don't go to JSONs
        # not a problem - we still have raw_date to get them back
        for seq in self.seqs.values():
            if 'date' in seq.attributes:
                del seq.attributes['date']
            if 'num_date' in seq.attributes:
                del seq.attributes['num_date']

        data = self.extras
        data["info"] = {
            "segment": self.segmentName,
            "n(starting)": self.nstart,
            "n(final)": len(self.seqs)
        }
        if self.segmentName == "genome":
            data["info"]["input_file"] = config["input_paths"][0]
        else:
            data["info"]["input_file"] = config["input_paths"][config["segments"].index(self.segmentName)]

        data["sequences"] = {}
        for seqName, seq in self.seqs.iteritems():
            data["sequences"][seqName] = {
                "attributes": seq.attributes,
                "seq": str(seq.seq)
            }

        json.dump(data, fh, indent=2)



if __name__=="__main__":
    pass
