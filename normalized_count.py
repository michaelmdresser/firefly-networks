import networkx as nx
from sklearn.cluster import KMeans

from collections import OrderedDict
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import logging
import time
from scipy.stats import norm

_FILE = "0.390625density0.1betadistributionTb_obstacles1600_steps_experiment_results_2020-11-09_11:05:25.960570_csv.csv"

def do_single_shuffle(sub_raster, voxel_bin_pairs):
    """
    Attempts one shuffle with retries (with inf loop check) in sub_raster

    :param sub_raster: a matrix keyed [voxel ID, (sliced) time bin ID] to make
                       the shuffle in
    :param voxel_bin_pairs: a set of (voxel ID, active time bin ID) pairs
                            (shuffle also performed here for consistency)
    :return: None, performs shuffle in references
    """

    def attempt_shuffle():
        # Pick 2 indexes to try a swap of
        templist = list(voxel_bin_pairs)
        chosen_indices = np.random.choice(range(len(templist)), size=2, replace=False)

        tup0 = templist[chosen_indices[0]]
        tup1 = templist[chosen_indices[1]]

        voxel0 = tup0[0]
        bin0 = tup0[1]

        voxel1 = tup1[0]
        bin1 = tup1[1]

        # The condition to reject a proposed shuffle
        if (voxel0, bin1) in voxel_bin_pairs or (voxel1, bin0) in voxel_bin_pairs:
            return False

        # This is a sanity check to make sure the list and the raster are staying
        # consistent. Not necessary for the functioning of the algorithm.
        if sub_raster[voxel0, bin0] != 1:
            raise Exception("sub raster does not have 1s correctly according to list: voxel0")
        if sub_raster[voxel1, bin1] != 1:
            raise Exception("sub raster does not have 1s correctly according to list: voxel1")


        # Swap voxel0's bin
        sub_raster[voxel0, bin0] = 0
        sub_raster[voxel0, bin1] = 1

        # Swap voxel1's bin
        sub_raster[voxel1, bin1] = 0
        sub_raster[voxel1, bin0] = 1

        newtup0 = (voxel0, bin1)
        newtup1 = (voxel1, bin0)
        voxel_bin_pairs.remove(tup0)
        voxel_bin_pairs.remove(tup1)
        voxel_bin_pairs.add(newtup0)
        voxel_bin_pairs.add(newtup1)

        return True

    attempts = 0
    max_attempts = 10
    # Try a single shuffle, returns false if the proposed shuffle has equal time bins
    # (the condition for a retry). This loop is to avoid an infinite loop in an edge
    # case in which, for some reason, the only possible shuffles all have the same time
    # bin (like a sub-raster consisting only of a single time bin).
    while attempts < max_attempts:
        if attempt_shuffle():
            return
        attempts += 1

    raise Exception("exceeded maximum shuffle attempts")


def shuffle_cascade(new_raster, min_bin, max_bin):
    """
    Does n_s shuffles of activation time bins in new_raster.

    :param new_raster: matrix keyed [voxel ID, time bin ID]
    :param min_bin: the bin to start shuffling at (inclusive)
    :param max_bin: the bin to stop shuffling at (inclusive)
    :return: None, performs shuffles in new_raster
    """

    # this is a VIEW, so edited the subraster will cause an edit
    # to new_raster
    # see: https://stackoverflow.com/questions/30917753/subsetting-a-2d-numpy-array
    sub_raster = new_raster[:, min_bin:max_bin + 1]

    # a set of tuples (voxel ID, (sliced) bin ID (so bin ID + min_bin is actual bin ID))
    voxel_bin_pairs = set()

    for voxel_id in range(sub_raster.shape[0]):
        for sliced_bin_id in range(sub_raster.shape[1]):
            if sub_raster[voxel_id, sliced_bin_id] == 1:
                voxel_bin_pairs.add((voxel_id, sliced_bin_id))


    # According to the paper, n_s ~= num active nodes in the cascade
    num_active_nodes = len(set(map(lambda x: x[0], voxel_bin_pairs)))
    n_s = num_active_nodes
    logging.debug("num active: %s" % num_active_nodes)
    for i in range(n_s):
        lenbefore = len(voxel_bin_pairs)
        do_single_shuffle(sub_raster, voxel_bin_pairs)
        lenafter = len(voxel_bin_pairs)

        if lenbefore != lenafter:
            raise Exception("broken invariant: reduced total activations")

    return sub_raster


def nc_shuffler(raster, clustered_timebins):
    """
    Takes the original raster and shuffles each cascade according to constrained
    pairwise shuffling, in which two voxels (active at some point during the cascade)
    are chosen and random and their time bins are swapped if they are not equal. If they
    are equal, a new pair is chosen.

    This swapping is performed n_s times per cascade, where n_s is the numbers of nodes
    active during that cascade.

    A new raster is returned based on the shuffled time bins.

    :param raster: 2d matrix, keyed [voxel ID, time bin ID]
    :param clustered_timebins: List of (time bin ID, cascade ID) pairs
    :return: new raster with shuffled time bins per cascade
    """

    new_raster = np.copy(raster)

    # build a dataframe of (time bin ID, cascade ID) for easier filtering to determine
    # min/max bin for each cascade
    cluster_bin_df = pd.DataFrame(clustered_timebins, columns=["binID", "cascadeID"])
    cascadeIDs = list(cluster_bin_df["cascadeID"].unique())
    for cascade_id in cascadeIDs:
        # determine min/max bin for the cascade
        filtered = cluster_bin_df.loc[cluster_bin_df["cascadeID"] == cascade_id]
        min_bin = filtered["binID"].min()
        max_bin = filtered["binID"].max()

        # sanity check
        if min_bin == max_bin:
            print("min and max bins are equal: %s, this shouldn't happen" % min_bin)
            raise Exception()

        shuffle_cascade(new_raster, min_bin, max_bin)

    return new_raster



class NormalizedCount:
    # Input:
    # (i)  A raster of activation data, recorded as (time,node) pairs,i.e. (te, se);
    # (ii) p-value for the reconstruction, p.

    # Output: The reconstructed weighted directed network
    def __init__(self, voxel_coords_to_ts, do_3d=False, time_bin_length=1):
        init_start = time.time()
        #################
        #### MAIN NC
        #################
        logging.info("building raster")
        self.raster, self.i_voxel_mapping = self.build_raster(voxel_coords_to_ts, do_3d=do_3d, time_bin_length=time_bin_length)
        raster_done = time.time()
        logging.info("done with raster, took: %s", raster_done - init_start)
        logging.info("building nc parameters, clustering")
        self.num_propagation_steps, self.ijs_at_each_timebin, self.ones_indices = self.count_coincident_components(self.raster)
        self.num_cascades, self.clustered_timesteps = self.count_cascades(self.ones_indices)
        params_done = time.time()
        logging.info("done with params, took: %s", params_done - raster_done)

        logging.info("starting NC")
        # TODO: this is a lazy hack to get only 1 NC
        self.nc_ij = self.normalized_count(self.raster, self.ijs_at_each_timebin, self.num_propagation_steps)
        nc_ij_done = time.time()
        logging.info("done with NC, took: %s", nc_ij_done - params_done)

        #################
        #### NULL MODEL
        #################
        logging.info("starting null model")
        # this is a list of nc results for the null model
        self.nc_r_ij = []

        # TODO: move these to class params?
        # over-shuffling factor
        self.f0 = 5
        # significance level
        self.p = 0.05

        N_r = int(self.f0 / self.p)
        logging.info("running NC on %s shuffled copies of raster for null model", N_r)

        durations = []
        started = time.time()
        for i in range(N_r):
            if i > 0 and i % 10 == 0:
                logging.info("average duration for shuffled nc_ij (finished %s) so far: %s", i, np.mean(durations))

            self.nc_r_ij.append(self.make_shuffled_nc_ij(self.raster, self.clustered_timesteps))
            finished = time.time()
            duration = finished - started
            durations.append(duration)
            started = finished

        ncrij_done = time.time()
        logging.info("done building nc^r_ij list, took: %s", ncrij_done - nc_ij_done)
        logging.info("building nc^p_ij")

        self.nc_p_ij = np.zeros((self.raster.shape[0], self.raster.shape[0]))
        nc_r_ij_3d = np.dstack(self.nc_r_ij) # 3d array keyed [voxel id, bin id, shuffle id]
        for i in range(self.nc_p_ij.shape[0]):
            for j in range(self.nc_p_ij.shape[1]):
                # we assume the PDF over nc^r_ij is normal
                mean = np.mean(nc_r_ij_3d[i, j, :])
                stdev = np.std(nc_r_ij_3d[i, j, :])

                # when mean is 0, all of the vals were 0
                # and constructing norm(0, 0) is a runtime warning
                if mean == 0:
                    self.nc_p_ij[i, j] = 0
                else:
                    dist = norm(loc=mean, scale=stdev)
                    threshold = dist.ppf(1 - self.p)

                    self.nc_p_ij[i, j] = threshold

        ncpij_done = time.time()
        logging.info("done building nc^p_ij, took %s", ncpij_done - ncrij_done)
        logging.info("building a_ij")

        # a_ij as defined in paper
        self.a_ij = np.zeros((self.raster.shape[0], self.raster.shape[0]))
        for i in range(self.a_ij.shape[0]):
            for j in range(self.a_ij.shape[1]):
                if self.nc_ij[i, j] > self.nc_p_ij[i, j]:
                    self.a_ij[i, j] = 1
                else:
                    self.a_ij[i, j] = 0

        aij_done = time.time()
        logging.info("done building a_ij, took %s", aij_done - ncpij_done)
        logging.info("building w_ij")

        # w_ij as defined in paper
        self.w_ij = np.zeros(self.a_ij.shape)
        for i in range(self.w_ij.shape[0]):
            for j in range(self.w_ij.shape[1]):
                self.w_ij[i, j] = self.a_ij[i, j]*(self.nc_ij[i, j] - self.nc_p_ij[i, j])

        wij_done = time.time()
        logging.info("done building w_ij, took %s", wij_done - aij_done)

        final_edge_count = np.sum(self.w_ij > 0)
        active_voxel_count = self.raster.shape[0]
        logging.info("total edges in w_ij: %s on voxel set size: %s", final_edge_count, active_voxel_count)

        init_finished = time.time()
        logging.info("done with full NC algorithm, took: %s", init_finished - init_start)

    @staticmethod
    def make_shuffled_nc_ij(base_raster, clustered_timesteps):
        # fortunately, clustering of timesteps should be independent of the raster
        # i.e. clustering of timesteps has nothing to do with the nodes they are
        # attached to
        shuffled_raster = nc_shuffler(base_raster, clustered_timesteps)

        num_propagation_steps, ijs_at_each_timebin, ones_indices = NormalizedCount.count_coincident_components(shuffled_raster)
        new_nc_ij = NormalizedCount.normalized_count(shuffled_raster, ijs_at_each_timebin, num_propagation_steps)
        return new_nc_ij


        # self.nc_ij = {}
        # for cascade in range(self.num_cascades):
        #     timesteps_in_cascade = [cts[0] for cts in self.clustered_timesteps if cts[1] == cascade]
        #     min_t = min(timesteps_in_cascade)
        #     max_t = max(timesteps_in_cascade)
        #     self.nc_ij[cascade] = self.normalized_count(min_t=min_t, max_t=max_t)
        # print(self.nc_ij)


    @staticmethod
    def build_raster(voxel_coords_to_ts, do_3d=False, time_bin_length=1):
        """Convert to raster for analysis

        :param voxel_coords_to_ts: voxel cooordinates and timesteps at which they are active
        :param do_3d: whether to do zcoord
        :param time_bin_length: Time delta
        :returns: raster of 1s and 0s for active time bins
        """
        raster_dict = OrderedDict()
        max_t = 0
        num_voxels = len(voxel_coords_to_ts.keys())
        for voxel, timesteps in voxel_coords_to_ts.items():
            if not do_3d:
                vox = (voxel[0], voxel[1])
            else:
                vox = voxel
            if max(timesteps) > max_t:
                max_t = max(timesteps) + 1
            key = min(timesteps)
            if not do_3d:
                raster_dict[(int(key), vox[0], vox[1])] = sorted(timesteps)
            else:
                raster_dict[(int(key), vox[0], vox[1], vox[2])] = sorted(timesteps)

        raster = np.zeros((num_voxels, int(max_t // time_bin_length)))
        if time_bin_length > 1:
            for voxel, timesteps in raster_dict.items():
                effective_time_bins = np.zeros(int(max_t // time_bin_length))
                for timestep in timesteps:
                    effective_time_bins[int(timestep // time_bin_length)] = 1
                raster_dict[voxel] = [float(time_bin) for time_bin in range(len(effective_time_bins))
                                      if effective_time_bins[time_bin] > 0]
        i_voxel_mapping = {}
        for i, voxel in enumerate(sorted(raster_dict.keys(), key=lambda x: x[0])):
            i_voxel_mapping[i] = voxel
            # i is the index in the Nt x N sparse matrix of the current voxel
            # 1s should indicate source nodes
            for timestep in raster_dict[voxel]:
                raster[i][int(timestep)] = 1
        return raster, i_voxel_mapping


    @staticmethod
    def count_cascades(flash_occurrences):
        """ Count the number of cascades and label timesteps to their cascade using kmeans

        :param flash_occurrences: Timesteps of flashes
        :return: number of clusters, and list of flash -> cascade label pairs
        """
        list_of_flash_timesteps = [fo[1] for fo in flash_occurrences]
        loft = np.array(list(set(list_of_flash_timesteps)))
        thresh = 9
        num_clusters = 0
        for i in range(len(loft) - 1):
            j = i + 1
            if loft[j] - loft[i] > thresh:
                num_clusters += 1
        km = KMeans(n_clusters=num_clusters)

        km.fit(loft.reshape(-1, 1))
        q = list(zip(loft, km.labels_))

        return num_clusters, q

    @staticmethod
    def count_coincident_components(raster):
        """

        :return: number of total propagations (flash moments crossed with coincident (flash at t+x) moments)
        :return: dict of potential propagation steps per timestep
        :return: list of (node, timestep) flash points (1s in the raster)
        """
        rows = raster.shape[0]
        cols = raster.shape[1]

        num_propagation_steps = 0
        ijs_at_each_timebin = {i: [] for i in range(cols)}
        ones_indices = []
        for timebin in range(0, cols):
            for voxel in range(0, rows):
                if raster[voxel, timebin] == 1:
                    ones_indices.append((voxel, timebin))

        for node_time_pair in ones_indices:
            timebin = node_time_pair[1]
            nxt_timebin = timebin + 1
            partner_list = [p for p in ones_indices if timebin < p[1] <= nxt_timebin]
            coincident_partnerships = [(node_time_pair[0], possible_partner[0]) for possible_partner in partner_list]
            ijs_at_each_timebin[timebin].extend(coincident_partnerships)

        for key in ijs_at_each_timebin.keys():
            if ijs_at_each_timebin.get(key+1) is not None:
                num_propagation_steps += (len(ijs_at_each_timebin[key]))

        return num_propagation_steps, ijs_at_each_timebin, ones_indices

    @staticmethod
    def normalized_count(raster, ijs_at_each_timebin, num_propagation_steps, min_t=None, max_t=None):
        """Not so inefficient now! Uses a mapping of t -> coincident t, t+1 flashers to calculate NC_ij

        NC[i,j] = NC_ij

        :param min_t: min timestep of current cascade (for NC for 1 cascade)
        :param max_t: max timestep of current cascade (for NC for 1 cascade)
        :return: 2d nc_ij array
        """
        nc = np.zeros((raster.shape[0], raster.shape[0]))
        for t in ijs_at_each_timebin.keys():
            if len(ijs_at_each_timebin.get(t)) == 0:
                continue
            elif min_t is not None and max_t is not None and (min_t > t or t > max_t):
                continue
            else:
                for (i, j) in ijs_at_each_timebin[t]:
                    nodes_active_at_time_t = set([x for (x, y) in ijs_at_each_timebin[t]])
                    nc_ij = (1.0 / len(nodes_active_at_time_t))
                    nc[i, j] += nc_ij
        normalized_counts = nc / num_propagation_steps
        return normalized_counts


class DataWrangler:
    def __init__(self, file, do_3d=False):
        filename = file
        if do_3d:
            self.df = pd.read_csv(filename, names=["x", "y", "z", "t"])
        else:
            self.df = pd.read_csv(filename, names=["x", "y", "t"])
        self.df["x_round"] = self.df["x"].apply(lambda x: round(x, 2))
        self.df["y_round"] = self.df["y"].apply(lambda x: round(x, 2))
        if do_3d:
            self.df["z_round"] = self.df["z"].apply(lambda x: round(x, 2))
        print("unique x:", len(self.df["x"].unique()))
        print("unique x rounded:", len(self.df["x_round"].unique()))
        print("unique y:", len(self.df["y"].unique()))
        print("unique y rounded:", len(self.df["y_round"].unique()))
        if do_3d:
            print("unique z:", len(self.df["z"].unique()))
            print("unique z rounded:", len(self.df["z_round"].unique()))

        self.min_x = self.df["x"].min()
        self.min_y = self.df["y"].min()
        if do_3d:
            self.min_z = self.df["z"].min()
        else:
            self.min_z = None
        self.voxel_length = 0.5

        voxels_to_activation_times = self.pair_voxels_with_activation_times(do_3d=do_3d)
        self.active_voxel_coords = list(map(lambda x: self.voxel_to_positions(x[0], x[1]),
                                            voxels_to_activation_times.keys()))

        self.real_voxels_to_activation_times = {self.voxel_to_positions(key[0], key[1]): ts
                                                for key, ts in voxels_to_activation_times.items()
                                                }

    @staticmethod
    def adjust_start_0(val, overall_min):
        """Adjusts a value's range to start at 0 so it can be cleanly divided for voxel number

        :param val: value to adjust
        :param overall_min: min value with which to adjust
        :return: adjusted value
        """
        if overall_min < 0:
            return val + (-overall_min)
        else:
            return val - overall_min

    def xyz_to_voxel_xyz(self, x, y, z=None):
        """Convert coords to voxel coords

        :param x: x coord
        :param y: y coord
        :param z: z coord (optional)
        :return:
        """
        x_adj = self.adjust_start_0(x, self.min_x)
        y_adj = self.adjust_start_0(y, self.min_y)
        z_adj = None
        if z is not None and self.min_z is not None:
            z_adj = self.adjust_start_0(z, self.min_z)

        if z_adj is not None:
            return x_adj // self.voxel_length, y_adj // self.voxel_length, z_adj // self.voxel_length
        else:
            return x_adj // self.voxel_length, y_adj // self.voxel_length, None

    def pair_voxels_with_activation_times(self, do_3d=False):
        """Do what it says

        :param do_3d: whether to use the z dimension
        :return: Dictionary, keys = voxels, values = time of the voxel's appearance
        """
        voxels_to_activation_times = {}

        for index, row in self.df.iterrows():
            x = row["x"]
            y = row["y"]
            t = row["t"]
            if do_3d:
                z = row["z"]
            else:
                z = None

            voxel = self.xyz_to_voxel_xyz(x, y, z)
            if voxel in voxels_to_activation_times:
                voxels_to_activation_times[voxel].append(t)
            else:
                voxels_to_activation_times[voxel] = [t]

        return voxels_to_activation_times

    def voxel_to_positions(self, vx, vy, vz=None):
        x = (vx * self.voxel_length) + self.min_x
        y = (vy * self.voxel_length) + self.min_y
        z = None
        if vz is not None and self.min_z is not None:
            z = (vz * self.voxel_length) + self.min_z

        return x, y, z


def visualize_voxels_and_points(voxeled, df, voxel_length):
    """Plots voxels

    :param voxeled: voxelified data
    :param df: actual data
    :param voxel_length: voxel length
    :return: figure and axis for plotting
    """
    figur, axys = plt.subplots(figsize=(12, 12))

    axys.scatter(df["x"], df["y"], s=1.5, alpha=1, color="blue")
    axys.set_xlabel("x")
    axys.set_ylabel("y")

    for x, y, z in voxeled:
        # https://stackoverflow.com/questions/37435369/matplotlib-how-to-draw-a-rectangle-on-image
        rect = patches.Rectangle((x, y),
                                 voxel_length,
                                 voxel_length,
                                 facecolor=None,
                                 edgecolor="r",
                                 linewidth=1,
                                 fill=False)
        axys.add_patch(rect)
    return figur, axys

# import sys
root = logging.getLogger()
root.setLevel(logging.INFO)

do_3d = False
dw_test = DataWrangler(_FILE, do_3d=do_3d)
normalized_count = NormalizedCount(dw_test.real_voxels_to_activation_times, do_3d=do_3d, time_bin_length=2)
# test_random_raster = nc_shuffler(raster, normalized_count.clustered_timesteps)

# normalized_count = NormalizedCount(dw_test.real_voxels_to_activation_times, do_3d=do_3d)
# test_random_raster = nc_shuffler(normalized_count.raster, normalized_count.clustered_timesteps)

# fig, ax = visualize_voxels_and_points(dw_test.real_voxels_to_activation_times, dw_test.df, dw_test.voxel_length)
# ax.set_title("Simulated with Voxel Length 0.5")
# plt.show()
