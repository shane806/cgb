"""Module for orthologous groups."""

import csv

from tqdm import tqdm

from . import misc
from . import visualization
from . import bayestraits_wrapper
from .my_logger import my_logger
from .misc import mean
from .blast import BLAST


class OrthologousGroup:
    """Class definition for OrthologousGroup.

    The OrthologousGroup class holds a group of genes which are orthologous to
    each other. Two genes are determined as orthologs if they are best BLAST
    hits for each other (reciprocal best BLAST hits).
    """
    def __init__(self, gene):
        self._genes = gene

    @property
    def genes(self):
        """Returns the list of orthologous genes."""
        return self._genes

    @property
    def description(self):
        """Returns the description for the genes in the group."""
        gene_descs = [gene.product for gene in self.genes if gene.product]
        return gene_descs[0] if gene_descs else ""

    def member_from_genome(self, genome_name):
        """Returns the member of the group from the given genome.

        Returns None if the specified genome has no genes in the group.
        """
        genes = [g for g in self.genes
                 if g.genome.strain_name == genome_name]
        if genes:
            return genes[0]
        return None

    def blast_eggnog_database(self, eggnog_blast_database):
        """BLAST against eggNOG database."""
        for gene in self.genes:
            blast_record = eggnog_blast_database.blastx(gene.to_fasta())
            print eggnog_blast_database.get_best_hit(blast_record)

    def discretize_regulation_states(self, phylo):
        """Discretizes the trait of regulation for all genes in the group.

        Each gene in the orthologous group has a posterior probability of
        regulation that is computed using the binding model. For each gene in
        the orthologous group, this method chooses one of the two possible
        states of the trait, (1) regulation and (2) not regulation, based on
        the posterior probability of the regulation of the gene.

        Returns: {string: int}: the dictionary containing (key, value) pairs
            where key is the TF accession number of the regulated gene, and the
            value is 0/1 indicating the regulation trait.
        """
        terminal_states = self.get_terminal_states(phylo)
        trait = {}
        for node in phylo.tree.get_terminals():
            states = ['1', '0', 'A']
            probabilities = [terminal_states[(node.name, state)]
                             for state in states]
            trait[node.name], = misc.weighted_choice(states, probabilities)
        return trait

    def bootstrap_traits(self, phylo, sample_size):
        """Sample discrete traits for each gene in the group.

        Each instance of the sample contains the discrete traits of regulation
        associated with each gene.
        """
        return [self.discretize_regulation_states(phylo)
                for _ in range(sample_size)]

    def get_terminal_states(self, phylo):
        states = {}
        genome_names = [node.name for node in phylo.tree.get_terminals()]
        for genome_name in genome_names:
            # Check if the group contains a gene from the current genome
            gene = self.member_from_genome(genome_name)
            if gene:
                p_reg = gene.operon.regulation_probability
                states[(genome_name, '1')] = p_reg
                states[(genome_name, '0')] = 1 - p_reg
                states[(genome_name, 'A')] = 0
            else:
                # No gene in this orthologous group from the genome
                states[(genome_name, '1')] = 0
                states[(genome_name, '0')] = 0
                states[(genome_name, 'A')] = 1
        return states

    def ancestral_state_reconstruction(self, phylo, sample_size=100):
        """Runs BayesTraits for ancestral state reconstruction.

        It estimates whether the gene is likely to be present in ancestral
        nodes, as well as its regulation, if the gene is present.
        """
        states = ['1', '0', 'A']
        bootstrap_inferred_states = {(node.name, state): 0
                                     for state in states
                                     for node in phylo.tree.get_nonterminals()}
        for trait in self.bootstrap_traits(phylo, sample_size):
            inferred_states = bayestraits_wrapper.bayes_traits(phylo, trait)
            for node in phylo.tree.get_nonterminals():
                for state in states:
                    k = (node.name, state)
                    bootstrap_inferred_states[k] += inferred_states.get(k, 0)
        # Normalize
        nonterminal_states = {k: v/sample_size
                              for (k, v) in bootstrap_inferred_states.items()}
        all_states = dict(self.get_terminal_states(phylo).items() +
                          nonterminal_states.items())
        # Store the ancestral states
        self._regulation_states = all_states

    @property
    def regulation_states(self):
        """Gets the field _regulation_states"""
        return self._regulation_states

    @property
    def prob_regulation_at_root(self):
        """Returns the probability of regulation at the root of the tree."""
        return self.regulation_states[('Root', '1')]

    def most_likely_state_at(self, node_name):
        """Returns the most likely state at the given node."""
        return max(['1', '0', 'A'],
                   key=lambda x: self.regulation_states[(node_name, x)])

    def ancestral_state_reconstruction_svg_view(self, phylo):
        temp_file = misc.temp_file_name(suffix='.svg')
        t = visualization.biopython_to_ete3(phylo.tree)
        visualization.view_by_gene(t, self, temp_file)
        with open(temp_file) as f:
            contents = f.read()
        return contents

    def __repr__(self):
        return str(self.genes)


# Class-associated functions
#
# The following functions provide the means to instantiate orthologous groups
# from a pre-defined subset of genes in all genomes under analysis and to
# export them in CSV format.


def construct_orthologous_groups(genes, genomes, cache):
    """Constructs orthologous groups starting with the given list of genes.

    For each genome, candidate genes that are identified as likely to be
    regulated are tagged for orthology detection.

    This constructor function receives the genome objects and the list
    of genes from each of these genomes on which reciprocal BLAST will be
    applied to infer orthologs.

    For each gene, it identifies the reciprocal best BLAST hits in other
    genomes and adds the gene and its orthologs to the orthologous group.
    Each orthologous group is a list of gene objects that have been found
    to be best-reciprocal BLAST hits.

    The function returns a list of orthologous groups.
    """

    #my_logger.info("Creating eggNOG database to BLAST.")
    #with open("/Users/sefa/Desktop/eggnog4.proteins.core_periphery.fa") as f:
    #    seq_fasta = f.read()
    #eggnog_blast_database = BLAST(seq_fasta, 'prot')

    groups = []
    for gene in tqdm(genes):
        # Check whether gene is already in a group, if it is, it skips the gene
        # (continue goes back to for loop beginning)
        if any(gene in grp.genes for grp in groups):
            continue
        # If gene not in any group, create list of orthologous genes by
        # performing reciprocal BLAST against all genomes that are not the
        # gene's own genome
        rbhs = [gene.reciprocal_blast_hit(other_genome, cache)
                for other_genome in genomes if gene.genome != other_genome]
        # Create the orthologous group with gene + orthologs on all other
        # genomes [if there are orthologs in the respective genomes]
        grp = OrthologousGroup([gene] + [rbh for rbh in rbhs if rbh])
        #grp.blast_eggnog_database(eggnog_blast_database)
        groups.append(grp)
    return groups


def orthologous_grps_to_csv(groups, filename):
    genomes = list(set(g.genome for grp in groups for g in grp.genes))
    with open(filename, 'w') as csvfile:
        csv_writer = csv.writer(csvfile)
        header_row = (['average_probability',
                       'average_probability_all',
                       'ortholog_group_size'] +
                      [field for genome in genomes
                       for field in ['probability (%s)' % genome.strain_name,
                                     'locus_tag (%s)' % genome.strain_name,
                                     'product (%s)' % genome.strain_name]])
        csv_writer.writerow(header_row)
        csv_rows = []
        for group in groups:
            genes = [group.member_from_genome(genome.strain_name)
                     for genome in genomes]
            # Average regulation probability
            avg_p = mean([gene.operon.regulation_probability
                          for gene in genes if gene])
            # Average regulation probability (p=0 for absent genes in the grp)
            avg_p_all = mean([gene.operon.regulation_probability
                              if gene else 0
                              for gene in genes])
            # Orthologous group size
            grp_size = len([gene for gene in genes if gene])
            row = [avg_p, avg_p_all, grp_size]
            for gene in genes:
                if gene:
                    row.extend(['%.3f' % gene.operon.regulation_probability,
                                gene.locus_tag, gene.product])
                else:
                    row.extend(['', '', ''])
            csv_rows.append(row)

        # Sort rows by average probability
        csv_rows.sort(key=lambda row: row[1], reverse=True)
        csv_writer.writerows(csv_rows)


def ancestral_state_reconstruction(ortho_grps, phylo):
    """Performs ancestral state reconstruction for all orthologous groups.

    Each orthologous group consists of genes (one from each genome) and
    associated posterior probabilities of regulation. Given each orthologous
    group, this method uses BayesTraits
    (http://www.evolution.rdg.ac.uk/BayesTraits.html) to estimate the state of
    regulation on internal nodes and the root of the phylogenetic tree.

    For each group, it randomly samples trees with discrete states on genes:
    regulation or not regulation. The discretization, setting each gene as
    regulated or not, is done proportional to posterior probability of the gene
    regulation.

    For each sampled tree with discrete states, BayesTraits performs the
    ancestral state reconstruction and computes the probability of regulation
    on each internal node and the root.. The final step is to average all
    probabilities from each run of BayesTraits on each sampled tree.

    Args:
        ortho_grps ([OrthologousGroup]): the list of orthologous groups
        genomes ([Genome]): the list of target genomes.
    Returns:
    """
    my_logger.info("Ancestral state reconstruction")
    for ortho_grp in tqdm(ortho_grps):
        ortho_grp.ancestral_state_reconstruction(phylo)
    my_logger.info("Ancestral state reconstruction [DONE]")


def ancestral_states_to_csv(ortho_grps, phylo, filename):
    with open(filename, 'w') as csvfile:
        csv_writer = csv.writer(csvfile)
        csv_writer.writerow(['genes', 'description'] +
                            ['%s P(%s)' % (node.name, state)
                             for node in phylo.tree.find_clades()
                             for state in ['1', '0', 'A']])
        for ortho_grp in ortho_grps:
            states = ortho_grp.regulation_states
            csv_writer.writerow(
                [', '.join(g.locus_tag for g in ortho_grp.genes),
                 ortho_grp.genes[0].product] +
                ['%.2f' % states[(node.name, state)]
                 for node in phylo.tree.find_clades()
                 for state in ['1', '0', 'A']])
