import cv2
import numpy as np
from tqdm import tqdm
from numba import njit
from scipy.spatial.distance import pdist, squareform
from typing import List
from joblib import Parallel, delayed
from scipy import ndimage
from scipy.optimize import minimize
from scipy.spatial.transform import Rotation
from scipy.sparse.csgraph import connected_components
import matplotlib.pyplot as plt
from matplotlib.path import Path
from matplotlib.patches import PathPatch
from matplotlib.colors import ListedColormap
import mpl_toolkits.mplot3d.art3d as art3d
from . import ray_trace
from .cutility import join_pairs
from .cstereo import match_v3, refractive_triangulate
from .cgreta import get_trajs_3d_t1t2, get_trajs_3d_t1t2t3, get_trajs_3d
from .camera import Camera
from .ellipse import cost_conic, cost_conic_triple, cost_circle_triple
from docplex.mp.model import Model
from itertools import product

dpi = 150

def see_corners(image_file, corner_number=(23, 15)):
    img = cv2.imread(image_file)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    ret, corners = cv2.findChessboardCorners(
            gray, corner_number,
            flags=sum((
                cv2.CALIB_CB_FAST_CHECK,
                cv2.CALIB_CB_ADAPTIVE_THRESH
                )))
    corners = np.squeeze(corners)
    plt.imshow(np.array(gray), cmap='gray')
    plt.plot(*corners.T[:2], color='tomato')
    plt.xlim(corners[:, 0].min() - 100, corners[:, 0].max() + 100)
    plt.ylim(corners[:, 1].min() - 100, corners[:, 1].max() + 100)
    plt.scatter(*corners[0].T, color='tomato')
    plt.axis('off')
    plt.show()


def plot_reproject(
        image, features, pos_3d, camera,
        filename=None, water_level=0, normal=(0, 0, 1)
        ):
    """
    Args:
        pos_3d (np.ndarray): 3d positions, shape (n, 3)
    """
    fig = plt.figure(figsize=(image.shape[1]/dpi, image.shape[0]/dpi), dpi=dpi)
    ax = fig.add_subplot(111)
    ax.imshow(image, cmap='gray')

    pos_2d = camera.project_refractive(pos_3d)
    ax.scatter(*pos_2d.T, color='tomato', marker='+', lw=1, s=128)

    ax.scatter(
            features[0], features[1],
            color='w', facecolor='none', alpha=0.5
            )

    plt.xlim(0, image.shape[1])
    plt.ylim(image.shape[0], 0)
    plt.gcf().set_frameon(False)
    plt.gcf().axes[0].get_xaxis().set_visible(False)
    plt.gcf().axes[0].get_yaxis().set_visible(False)
    plt.axis('off')
    if not filename:
        plt.show()
    else:
        plt.savefig(filename, bbox_inches='tight', pad_inches=0)
    plt.close()


def plot_reproject_with_roi(
        image, roi, features, pos_3d, camera,
        filename=None, water_level=0, normal=(0, 0, 1)
        ):
    fig = plt.figure(figsize=(image.shape[1]/dpi, image.shape[0]/dpi), dpi=dpi)
    ax = fig.add_subplot(111)
    ax.imshow(image, cmap='gray')

    for point in pos_3d:
        xy = ray_trace.reproject_refractive(point, camera)
        ax.scatter(*xy, color='tomato', marker='+', lw=1, s=128)

    ax.scatter(
            features[0] + roi[1].start, features[1] + roi[0].start,
            color='w', facecolor='none', alpha=0.5
            )

    plt.xlim(0, image.shape[1])
    plt.ylim(image.shape[0], 0)
    plt.gcf().set_frameon(False)
    plt.gcf().axes[0].get_xaxis().set_visible(False)
    plt.gcf().axes[0].get_yaxis().set_visible(False)
    plt.axis('off')
    if not filename:
        plt.show()
    else:
        plt.savefig(filename, bbox_inches='tight', pad_inches=0)
    plt.close()


def get_clusters(image, threshold, min_size, roi):
    """
    apply threshold to image and label disconnected part
    small labels (size < min_size) were erased
    the coordinates of labels were returned, shape: (label_number, 3)
    """
    binary = image > (image[roi].max() * threshold)
    mask = np.zeros(image.shape, dtype=int)
    mask[roi] = 1
    labels, _ = ndimage.label(binary)
    labels *= mask
    counted = np.bincount(labels.ravel())
    noise_indice = np.where(counted < min_size)
    mask = np.in1d(labels, noise_indice).reshape(image.shape)
    labels[mask] = 0
    labels, _ = ndimage.label(labels)
    clusters = []
    for value in np.unique(labels.ravel()):
        if value > 0:
            cluster = np.array(np.where(labels == value)).T
            clusters.append(cluster)
    return clusters


def plot_epl(line: List[float], image: np.ndarray):
    """
    Get the scatters of an epipolar line *inside* an image
    The images is considered as being stored in (row [y], column [x])
    """
    u = np.linspace(0, image.shape[1], 1000)
    v = -(line[2] + line[0] * u) / line[1]
    mask = np.ones(len(u), dtype=bool)
    mask[v < 0] = False
    mask[v >= image.shape[0]] = False
    uv = np.vstack([u, v]).T[mask]
    uv_dst = uv
    mask = np.ones(len(uv_dst), dtype=bool)
    mask[uv_dst[:, 0] < 0] = False
    mask[uv_dst[:, 0] >= image.shape[1]] = False
    mask[uv_dst[:, 1] < 0] = False
    mask[uv_dst[:, 1] >= image.shape[0]] = False
    epl = uv_dst[mask]
    return epl.T


def get_ABCD(corners, width, excess_rows) -> dict:
    """
    What is ABCD?

    .. code-block:: none

        C +-------+ D
          |       |
          |       |
          |       |
        A +-------+ B

    (AB // CD, AC // BD)

    Args:
        corners (`numpy.ndarray`): the coordinates of chessboard corners found by
                                        ``cv2.findChessboardCorners``, shape (n, 2)
        width (`int`): the number of corners in a row
        excess_rows(`int`): if the chessboard is make of (m, n) corners (m > n)
                                 then n is *width*, excess_rows = m - n

    Return:
        `dict`: the locations of A, B, C, D respectively.
    """
    points_Ah, points_Bh, points_Ch, points_Dh = [], [], [], []
    for er in range(excess_rows + 1):
        start_AB = er * width
        start_CD = start_AB + width * (width - 1)
        offset = width - 1
        A = corners[start_AB].tolist()
        B = corners[start_AB + offset].tolist()
        C = corners[start_CD].tolist()
        D = corners[start_CD + offset].tolist()
        # collect the points
        Ah = A + [1]
        Bh = B + [1]
        Ch = C + [1]
        Dh = D + [1]
        points_Ah.append(Ah)  # (n, 3)
        points_Bh.append(Bh)  # (n, 3)
        points_Ch.append(Ch)  # (n, 3)
        points_Dh.append(Dh)  # (n, 3)
    ABCD = {'A': np.array(points_Ah),
            'B': np.array(points_Bh),
            'C': np.array(points_Ch),
            'D': np.array(points_Dh)}
    return ABCD


def get_affinity(abcd):
    """
    Getting the affinity matrix from a set of corners measured from a\
        chessboard image

    what is ABCD?

    .. code-block:: none

        C +-------+ D
          |       |
          |       |
          |       |
        A +-------+ B

    Args:
        abcd (`dict`): the measured coordinates of chessboard corners

    Return:
        `numpy.ndarray`: the affine transformation
    """
    lAB = np.cross(abcd['A'], abcd['B'])  # (n, 3)
    lCD = np.cross(abcd['C'], abcd['D'])  # (n, 3)
    lAC = np.cross(abcd['A'], abcd['C'])  # (n, 3)
    lBD = np.cross(abcd['B'], abcd['D'])  # (n, 3)
    points_inf_1 = np.cross(lAB, lCD)
    points_inf_1 = (points_inf_1/points_inf_1[:, -1][:, None])[:, :2]  # sorry
    points_inf_2 = np.cross(lAC, lBD)
    points_inf_2 = (points_inf_2/points_inf_2[:, -1][:, None])[:, :2]  # (n, 2)

    points_inf = np.vstack(
            (points_inf_1, points_inf_2)  # [(n, 2), (n, 2)] -> (2n, 2)
            )
    a, b = np.polyfit(*points_inf.T, deg=1)
    H_aff = np.array([
        [1, 0, 0],
        [0, 1, 0],
        [a/b, -1/b, 1]
    ])
    return H_aff


def get_similarity(abcd, H_aff):
    """
    Getting the similarity matrix from a set of corners measured from

    What is ABCD?

    .. code-block:: none

        C +-------+ D
          |       |
          |       |
          |       |
        A +-------+ B

    Args:
        abcd (`dict`): the measured coordinates of chessboard corners
        H_aff (`numpy.ndarray`): affinity that makes coordinates affinely\
            recitified

    Return:
        `numpy.ndarray`: the similar transformation matrix
    """
    abcd_aff = {}
    for letter in abcd:
        aff_rec = H_aff @ abcd[letter].T  # (3, n)
        aff_rec = aff_rec / aff_rec[-1]
        abcd_aff.update({letter: aff_rec.T})
    lAB = np.cross(abcd_aff['A'], abcd_aff['B'])  # (n, 3)
    lAC = np.cross(abcd_aff['A'], abcd_aff['C'])  # (n, 3)
    lAD = np.cross(abcd_aff['A'], abcd_aff['D'])  # (n, 3)
    lBC = np.cross(abcd_aff['B'], abcd_aff['C'])  # (n, 3)
    lAB = (lAB / lAB[:, -1][:, None])[:, :2]
    lAC = (lAC / lAC[:, -1][:, None])[:, :2]
    lAD = (lAD / lAD[:, -1][:, None])[:, :2]
    lBC = (lBC / lBC[:, -1][:, None])[:, :2]
    # solve circular point constrain from 2 pp lines
    s11_ensemble, s12_ensemble = [], []
    M = np.empty((2, 3))
    for i in range(len(abcd['A'])):  # for different "point A"
        M[0, 0] = lAB[i, 0] * lAC[i, 0]
        M[0, 1] = lAB[i, 0] * lAC[i, 1] + lAB[i, 1] * lAC[i, 0]
        M[0, 2] = lAB[i, 1] * lAC[i, 1]
        M[1, 0] = lAD[i, 0] * lBC[i, 0]
        M[1, 1] = lAD[i, 0] * lBC[i, 1] + lAD[i, 1] * lBC[i, 0]
        M[1, 2] = lAD[i, 1] * lBC[i, 1]
        s11, s12 = np.linalg.solve(M[:, :2], -M[:, -1])
        s11_ensemble.append(s11)
        s12_ensemble.append(s12)
    S = np.array([
            [np.mean(s11_ensemble), np.mean(s12_ensemble)],
            [np.mean(s12_ensemble), 1],
        ])
    S = S / max(s11, 1)  # not scaling up the coordinates
    K = np.linalg.cholesky(S)
    Hs_inv = np.array([  # from similar to affine
        [K[0, 0], K[0, 1], 0],
        [K[1, 0], K[1, 1], 0],
        [0, 0, 1]
        ])
    return np.linalg.inv(Hs_inv)


def get_corners(image: np.array, rows: int, cols: int, camera_model=None):
    """
    use findChessboardCorners in opencv to get coordinates of corners
    """
    ret, corners = cv2.findChessboardCorners(
        image, (rows, cols),
        flags=sum((
            cv2.CALIB_CB_FAST_CHECK,
            cv2.CALIB_CB_ADAPTIVE_THRESH
        ))
    )
    corners = np.squeeze(corners)  # shape (n, 1, 2) -> (n, 2)

    # undistorting points does make l_inf fit better
    if not isinstance(camera_model, type(None)):
        corners = camera_model.undistort_points(corners, want_uv=True)

    return corners


def get_homography_image(image, rows, cols, camera_model=None):
    """
    get the homography transformation from an image with a chess-board

    Args:
        image (`numpy.ndarray`): a 2d image
        rows (`int`): the number of *internal corners* inside each row
        cols (`int`): the number of *internal corners* inside each column
        camera_model (Camera): (optional) a Camera instance that stores the
                  distortion coefficients of the lens
    """
    length = max(rows, cols)  # length > width
    width = min(rows, cols)

    corners = get_corners(image, width, length, camera_model)
    excess_rows = length - width

    abcd = get_ABCD(corners, width, excess_rows)

    H_aff = get_affinity(abcd)
    H_sim = get_similarity(abcd, H_aff)

    return H_sim @ H_aff


def get_homography(camera, angle_num=10):
    """
    Get the homography that simiarly recover the 2d image\
        perpendicular to z-axis

    Args:
        camera (Camera): a Camera instance of current camera
        angle_num (`int`): a virtual chessboard is rotated angle_num\
            times for calculation
    """
    angles = np.linspace(0, np.pi/2, angle_num)  # rotation_angle

    abcd = {
        'A': np.empty((angle_num, 3)),
        'B': np.empty((angle_num, 3)),
        'C': np.empty((angle_num, 3)),
        'D': np.empty((angle_num, 3)),
    }

    for i, t in enumerate(angles):
        R = np.array((  # rotation matrix pp z-axis
            (np.cos(t), -np.sin(t), 0),
            (np.sin(t), np.cos(t), 0),
            (0, 0, 1)
        ), dtype=np.float32)

        abcd_3d = np.array((  # shape (3 dim, 4 corners)
            (0, 0, 0),
            (1, 0, 0),
            (0, 1, 0),
            (1, 1, 0),
        ), dtype=np.float32).T

        abcd_3d[:2, :] -= abcd_3d[:2, :].mean(axis=1)[:, np.newaxis]

        abcd_3d_rot = R @ abcd_3d  # shape (3 dim, 4 corners)

        # shape (4 dim, 4 corners)
        abcd_3d_rot_h = np.vstack((abcd_3d_rot, np.ones((1, 4))))

        abcd_2dh = camera.p @ abcd_3d_rot_h
        abcd_2dh = (abcd_2dh / abcd_2dh[-1, :]).T  # shape (4 corners, 3 dim)

        abcd['A'][i] = abcd_2dh[0]
        abcd['B'][i] = abcd_2dh[1]
        abcd['C'][i] = abcd_2dh[2]
        abcd['D'][i] = abcd_2dh[3]

    H_aff = get_affinity(abcd)
    H_sim = get_similarity(abcd, H_aff) @ H_aff

    return H_sim


def update_orientation(orientations, locations, H, length=10):
    """
    Calculate the orientatin after applying homography H
    This is function is used to get a 'recitified' orientation

    Args:
        orientation (`numpy.ndarray`): angles of the fish,\
            sin(angle) -> x very sadly
        locatons (`numpy.ndarray`): xy positons of fish in\
            the image, not row-col
        H (`numpy.ndarray`): the homography matrix
        length (`int`): length of the orientation bar

    Return:
        `numpy.ndarray`: the recitified orientations
    """
    orient_rec = []
    for o, xy in zip(orientations, locations):
        p1h = np.array((
            xy[0] - length * np.sin(o),
            xy[1] - length * np.cos(o), 1
        ))
        p2h = np.array((
            xy[0] + length * np.sin(o),
            xy[1] + length * np.cos(o), 1
        ))
        p1h = H @ p1h
        p2h = H @ p2h
        p1 = (p1h / p1h[-1])[:2]
        p2 = (p2h / p2h[-1])[:2]
        o_rec = np.arctan2(*(p2 - p1))
        orient_rec.append(o_rec)
    orient_rec = np.array(orient_rec)
    orient_rec[orient_rec < 0] += np.pi
    orient_rec[orient_rec > np.pi] -= np.pi
    return orient_rec


def get_orient_line(locations, orientations, length=10):
    """
    Get the line for plot the orientations

    Args:
        locations (`numpy.ndarray`): shape (n, 2)
        orientations (`numpy.ndarray`): shape (n, )
    """
    unit_vector = np.array((np.sin(orientations), np.cos(orientations)))
    oline_1 = locations - length * unit_vector
    oline_2 = locations + length * unit_vector

    olines_x = []
    olines_y = []

    for i in range(oline_1.shape[1]):
        olines_x += [oline_1[0, i], oline_2[0, i], np.nan]
        olines_y += [oline_1[1, i], oline_2[1, i], np.nan]
    return np.array((olines_x, olines_y))


@njit(fastmath=True)
def polar_chop(image, H_sim, centre, radius, n_angle, n_radius, dist_coef, k):
    """
    Chop an image in the polar coordinates
    return the chopped result as a labelled image

    Args:
        image (`numpy.ndarray`): 2d image as a numpy array
        H_sim (`numpy.ndarray`): a homography (3 x 3 matrix) to similarly\
            rectify the image
        centre (`numpy.ndarray`): origin of the polar coordinate system
        radius (`int`): maximum radius in the polar coordinate system
        n_angle (`int`): number of bins in terms of angle
        n_radius (`int`): number of bins in terms of radius
        dist_coef (`numpy.ndarray`): distortion coefficients of the camera\
            , shape (5, ), k1, k2, p1, p2, k3 (from opencv by default)
        k: (`numpy.ndarray`) camera calibration matrix (bible, P155)

    Return:
        `numpy.array`: labelled image where each chopped regions were labelled\
            with different values
    """
    # setting up bin edges
    be_angle = np.linspace(0, 2 * np.pi, n_angle+1)  # bin_edge
    r0 = np.sqrt(radius**2 / (n_angle * (n_radius-1) + 1))
    be_radius = np.empty(n_radius+1)
    be_radius[0] = 0
    be_radius[1] = r0
    for i in range(2, n_radius+1):
        be_radius[i] = np.sqrt(((i-1) * n_angle + 1))* r0
    be_r2 = be_radius ** 2

    # getting camera parameters
    k1, k2, p1, p2, k3 = dist_coef
    fx, fy, cx, cy = k[0, 0], k[1, 1], k[0, 2], k[1, 2]

    result = np.empty(image.shape, dtype=np.uint64)
    for x in range(image.shape[1]):  # x -> col
        for y in range(image.shape[0]):  # y -> row!
            # undistort x, y, shamelessly copied from opencv
            x0 = (x - cx) / fx
            y0 = (y - cy) / fy
            x_ud, y_ud = x0, y0  # ud -> undistorted
            for _ in range(5):  # iter 5 times
                r2 = x_ud**2 + y_ud ** 2
                k_inv = 1 / (1 + k1 * r2 + k2 * r2**2 + k3 * r2**3)
                delta_x = 2 * p1 * x_ud*y_ud + p2 * (r2 + 2 * x_ud**2)
                delta_y = p1 * (r2 + 2 * y_ud**2) + 2 * p2 * x_ud*y_ud
                x_ud = (x0 - delta_x) * k_inv
                y_ud = (y0 - delta_y) * k_inv
            x_ud = x_ud * fx + cx
            y_ud = y_ud * fy + cy

            # similar rectification
            xyh = np.array([x_ud, y_ud, 1], dtype=np.float64)
            xyh_sim = H_sim @ xyh
            xy_sim = (xyh_sim / xyh_sim[-1])[:2]  # similar trasnformed
            x_sim, y_sim = xy_sim - centre

            # work in polar coordinates
            t = np.arctan2(y_sim, x_sim) + np.pi  # theta
            r2 = x_sim**2 + y_sim**2

            # label the image
            if r2 <= be_r2[1]:  # assign central part to value 1
                result[y, x] = 1
            elif r2 <= be_r2[-1]:
                idx_angle, idx_radius = 0, 0
                for i, a in enumerate(be_angle[1:]):
                    if (t < a) and (t >= be_angle[i]):
                        idx_angle = i
                for j, r2e in enumerate(be_r2[1:]):
                    if (r2 < r2e) and (r2 >= be_r2[j]):
                        idx_radius = j
                idx_group = idx_angle + (idx_radius - 1) * n_angle + 2
                result[y, x] = idx_group
            else:
                result[y, x] = 0
    return result


def get_polar_chop_spatial(radius, n_angle, n_radius):
    """
    Retrieve the correspondance between the values from :any:`polar_chop`
        and the radius/angle values.

    Args:
        radius (`int`): maximum radius in the polar coordinate system
        n_angle (`int`): number of bins in terms of angle
        n_radius (`int`): number of bins in terms of radius

    Return:
        `dict`: { label_value : (angle, radius), ... }
    """
    result = {}
    be_angle = np.linspace(0, 2 * np.pi, n_angle+1)  # be = bin edge
    r0 = np.sqrt(radius**2 / (n_angle * (n_radius-1) + 1))
    be_radius = np.empty(n_radius+1)
    be_radius[0] = 0
    be_radius[1] = r0
    for i in range(2, n_radius+1):
        be_radius[i] = np.sqrt(((i-1) * n_angle + 1))* r0
    bc_angle = (be_angle[1:] + be_angle[:-1]) / 2  # bc = bin centre
    bc_radius = (be_radius[1:] + be_radius[:-1]) / 2
    value = 1
    result.update({value : (bc_radius[0], np.nan)})
    for cr in bc_radius[1:]:
        for ca in bc_angle:
            value += 1
            result.update({value : (cr, ca)})
    return result


def get_indices(labels):
    indices = []
    for val in set(np.ravel(labels)):
        if val > 0:
            indices.append(
                np.array(np.where(labels.ravel() == val)[0])
            )
    return indices


def box_count_polar_image(image, indices, invert=False, rawdata=False):
    """
    Calculate the average density inside different regions inside an image

    Args:
        image (`numpy.ndarray`): the image taken by the camera without undistortion
        indices (`numpy.ndarray`): labelled image specifying different box regions
    """
    if invert:
        data = image.max() - image.ravel()
    else:
        data = image.ravel()
    intensities = np.empty(len(indices))
    for i, idx in enumerate(indices):
        pixels = data[idx]
        intensities[i] = np.mean(pixels)
    if rawdata:
        return intensities
    else:
        return np.std(intensities), np.min(intensities), np.mean(intensities)


def box_count_polar_video(
        video, labels, cores=2, report=True, invert=False, rawdata=False
):
    if report:
        to_iter = tqdm(video)
    else:
        to_iter = video
    indices = get_indices(labels)
    results = Parallel(n_jobs=cores, require='sharedmem')(
        delayed(
            lambda x: box_count_polar_image(x, indices, invert, rawdata)
        )(frame) for frame in to_iter
    )
    return np.array(results).T  # (3, n)


def get_overlap_pairs(trajs, num, rtol):
    """
    Args:
        trajs (`list`): a collections of trajectories, each trajectory is
            a tuple, containing (positions [N, 3], reprojection_error)
        num (`int`): the maximum number of allowed overlapped objects
        rtol (`float`): the minimum distance between two non-overlapped objects

    Return:
        `list` of `tuple`: the indices of overlapped objects
    """
    N, T = len(trajs), len(trajs[0][0])
    overlap_pairs = []
    dist_mat = np.empty((N, N, T))
    rtol_sq = rtol * rtol

    for i, t1 in enumerate(trajs):
        for j, t2 in enumerate(trajs):
            dist_mat[i, j] = np.sum(np.square(t1[0] - t2[0]), axis=1)

    with np.errstate(invalid='ignore'):  # ignore the case like NAN < 5
        adj_mat = np.sum(dist_mat < rtol_sq, axis=2) >= num

    adj_mat = np.triu(adj_mat, k=1)
    return np.array(np.nonzero(adj_mat)).T


def convert_traj_format(traj, t0):
    """
    Converting from (positions, error) to (time, positions)
    The starting & ending NAN will be removed, the NAN in the middile will
    be replaced by linear interpolation

    Args:
        traj (`tuple`): one trajectory, represented by (positions, error)
        t0 (`int`) : the starting frame of this trajectory

    Return:
        `list` of `tuple`: a list of ONE trajectory
            represented by (time, positions)
    """
    # detect head NAN
    was_nan = True
    new_traj = [[], []]

    coordinates = traj[0].copy()
    is_valid_array = np.logical_not(np.isnan(coordinates[:, 0]))
    is_valid_array = fill_hole_1d(is_valid_array, size=3)
    coordinates = interpolate_nan(coordinates)
    coordinates[~is_valid_array] = np.nan

    for t, coord in enumerate(coordinates):
        is_nan = np.isnan(coord).any()
        #if was_nan and (not is_nan):
        #    new_trajs.append([[], []])
        if (not is_nan):
            new_traj[0].append(t0 + t)
            new_traj[1].append(coord)
        was_nan = is_nan

    return [ (np.array(new_traj[0]), np.array(new_traj[1])) ]


def fill_hole_1d(binary, size):
    """
    Fill "holes" in a binary signal whose length is smaller than size

    Args:
        binary (`numpy.ndarray`): a boolean numpy array
        size (`int`): the holes whose length is smaller than size
            will be filled

    Return:
        `numpy.ndarray`: the filled binary array

    Example:
        >>> binary = np.array([0, 1, 1, 0, 0, 0, 0, 1, 0, 0, 1, 0, 1])
        >>> filled = np.array([0, 1, 1, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1])
        >>> np.array_equal(filled, fill_hole_1d(binary, 2))
        True
    """
    filled = binary.copy().astype(int)
    for s in np.arange(size + 2, 2, -1):
        probe = np.zeros(s)
        probe[0] = 1
        probe[-1] = 1
        for shift in range(0, len(binary) - s + 1):
            if np.array_equal(probe, filled[shift : shift + s]):
                filled[shift : shift + s] = 1
    return filled.astype(binary.dtype)


def interpolate_nan(coordinates):
    """
    replace nan with linear interpolation of a (n, 3) array along the first axis

    Args:
        coordinates (`numpy.ndarray`): xyz coordinates of a trajectory,\
            might contain nan
        is_nan (`numpy.ndarray`): 1d bolean array showing if coordinates[i]\
            is nan or not

    Return:
        `numpy.ndarray`: the interpolated coordinates array

    Example:
        >>> target = np.array([np.arange(100)] * 3).T.astype(float)
        >>> target.shape
        (100, 3)
        >>> with_nan = target.copy()
        >>> nan_idx = np.random.randint(0, 100, 50)
        >>> with_nan[nan_idx] = np.nan
        >>> with_nan[0] = 0
        >>> with_nan[-1] = 99
        >>> np.allclose(target, interpolate_nan(with_nan))
        True
    """
    is_nan = np.isnan(coordinates[:, 0])
    for head_idx in range(len(coordinates)):
        if not is_nan[head_idx]:
            break
    # detect tail NAN
    for tail_idx in range(len(coordinates))[::-1]:
        if not is_nan[tail_idx]:
            break
    valid_range = slice(head_idx, tail_idx + 1, 1)
    result = coordinates.copy()
    nan_indices = is_nan[valid_range].nonzero()[0]
    not_nan_indices = np.logical_not(is_nan[valid_range]).nonzero()[0]
    for dim in range(3):
        result[:, dim][valid_range][nan_indices] = np.interp(
            nan_indices,
            not_nan_indices,
            coordinates[:, dim][valid_range][not_nan_indices]
        )
    return result


def get_valid_ctraj(trajectories, z_min, z_max):
    """
    Extract valid trajectories (inside box boundary) from raw trajectories

    Args:
        trajectories (`list` [ ( `numpy.ndarray`, `float` ) ]):\
            a collection of trajectories, each trajectory is (positions , error)
        z_min (`float`): the minimum allowed z-coordinate of each trajectory\
            corresponding to the bottom of the boundary.
        z_max (`float`): the maximum allowed z-coordinate of each trajectory\
            corresponding to the top of the boundary.

    Return:
        `list` [ ( `numpy.ndarray`, `float` ) ]: valid trajectories
    """
    valid_trajectories = []
    is_valid = True
    for t in trajectories:
        z = t[0].T[-1]
        z = z[~np.isnan(z)]
        is_valid *= (z < z_max).all()  # all fish under water
        is_valid *= (z > z_min).all()  # all fish in the tank
        is_valid *= len(z) > 1
        if is_valid:
            valid_trajectories.append(t)
    return valid_trajectories


def post_process_ctraj(trajs_3d, t0, z_min, z_max, num=5, rtol=10):
    """
    Refining the trajectories obtained from cgreta, following three steps

        1. Removing trajectories that is outside the boundary (fish tank).
        2. Removing trajectories that overlaps. Overlapping means for\
           2 trajectories, there are more than :py:data:`num` positions\
           whose distance is below :py:data:`rtol`
        3. Convert the format of the trajectory, from (position, error)\
           to (time, position)

    Args:
        trajs_3d (`list` [ ( `numpy.ndarray`, `float` ) ]):\
            a collection of trajectories, each trajectory is (positions , error)
        t0 (`int`): the starting frame of these trajectories.
        z_min (`float`): the minimum allowed z-coordinate of each trajectory\
            corresponding to the bottom of the boundary.
        z_max (`float`): the maximum allowed z-coordinate of each trajectory\
            corresponding to the top of the boundary.
        num (`int`): the maximum number of allowed overlapped positions.

    Return:
        `list` [ (`numpy.ndarray`, `numpy.ndarray`) ]:\
        a collection of refined trajectories, represented as (time, position)
    """
    # remove trajectories that is outside the tank
    trajs_3d_filtered = get_valid_ctraj(trajs_3d, z_min, z_max)
    if len(trajs_3d_filtered) == 0:
        return []

    # join overlapped trajectories
    op = get_overlap_pairs(trajs_3d_filtered, num, rtol)
    jop = join_pairs(op)
    if len(jop) == 0:
        trajs_3d_opt = []
        for traj in trajs_3d_filtered:
            trajs_3d_opt += convert_traj_format(traj, t0)
        return trajs_3d_opt
    trajs_3d_opt = []

    not_unique = np.hstack(jop).ravel().tolist()
    for i, traj in enumerate(trajs_3d_filtered):
        if i not in not_unique:
            trajs_3d_opt += convert_traj_format(traj, t0)
    for p in jop:
        best_idx = np.argmin([trajs_3d_filtered[idx][1] for idx in p])
        chosen_traj = trajs_3d_filtered[p[best_idx]]
        trajs_3d_opt += convert_traj_format(chosen_traj, t0)
    return trajs_3d_opt


def get_short_trajs(
        cameras, features_mv_mt, st_error_tol, search_range, t1, t2,
        z_min, z_max, overlap_num, overlap_rtol, reproj_err_tol, t3=1
    ):
    """
    Getting short 3D trajectories from 2D positions and camera informations

    Args:
        cameras (Camera): cameras for 3 views
        freatrues_mv_mt (`list`): 2d features in different views\
            at different frames
        st_error_tol (`float`): the stereo reprojection error cut-off\
            for stereo linking
        tau (`int`): the length of trajectories in each batch; unit: frame
            the overlap between different batches will be `tau // 2`
        z_min (`float`): the minimum allowed z-values for all trajectories
        z_max (`float`): the maximum allowed z-values for all trajectories
        t1 (`int`): the time duration in the first iteration in GReTA
        t2 (`int`): the time duration in the second iteration in GReTA
        t3 (`int`): the time duration in the third iteration in GReTA
        overlap_num (`int`): if two trajectories have more numbers of
            overlapped positions than `overlap_num`, establish a link.
        overlap_rtol (`float`): if two trajectories have more numbers of
            overlapped positions, link them.
        reproj_err_tol (`float`): the 3d positiosn whose reprojection error
            is greater than this will not be re-constructed, instead a NAN
            is inserted into the trajectory

    Return:
        `list` [ (`numpy.ndarray`, `float`) ]:
            trajectories
    """
    shift = t1 * t2 * t3
    frame_num = len(features_mv_mt)
    t_starts = [t * shift for t in range(frame_num // shift)]
    proj_mats = [cam.p for cam in cameras]
    cam_origins = [cam.o for cam in cameras]
    trajectories = []
    for t0 in t_starts:
        stereo_matches = []
        for features_mv in features_mv_mt[t0 : t0 + shift]:
            matched = match_v3(
                *features_mv, *proj_mats, *cam_origins,
                tol_2d=st_error_tol, optimise=True
            )
            stereo_matches.append(matched)
        features_mt_mv = []  # shape (3, frames, n, 3)
        for view in range(3):
            features_mt_mv.append([])
            for frame in range(t0, t0 + shift):
                features_mt_mv[-1].append( features_mv_mt[frame][view] )
        if (t2 == 1 and t3 == 1):
            ctrajs_3d = get_trajs_3d(
                features_mt_mv, stereo_matches, proj_mats, cam_origins,
                c_max=500, search_range=search_range, re_max=reproj_err_tol
            )
        elif (t2 > 1 and t3 == 1):
            ctrajs_3d = get_trajs_3d_t1t2(
                features_mt_mv, stereo_matches, proj_mats, cam_origins,
                c_max=500, search_range=search_range,
                search_range_traj=search_range, tau_1=t1, tau_2=t2,
                re_max=reproj_err_tol
            )
        elif (t2 > 1 and t3 > 1):
            ctrajs_3d = get_trajs_3d_t1t2t3(
                features_mt_mv, stereo_matches, proj_mats, cam_origins,
                c_max=500, search_range=search_range,
                search_range_traj=search_range, tau_1=t1, tau_2=t2, tau_3=t3,
                re_max=reproj_err_tol
            )
        else:
            raise ValueError("unsatisfied condition: t3 > 1")
        trajs_3d_opt = post_process_ctraj(
            ctrajs_3d, t0, z_min, z_max,
            overlap_num, overlap_rtol
        )
        trajectories += trajs_3d_opt
    return [t for t in trajectories if len(t[0]) > 1]


def remove_spatial_overlap(trajectories, ntol, rtol):
    """
    If two trajectories were overlap in the space, choose the one\
        with minimum reprojection error.

    Args:
        trajectories (`list` of (`numpy.ndarray`, `float`)): a collection\
            of trajectories, each trajectory is (positions, reprojection_error)
        ntol (`int`): if two trajectories have more numbers of overlapped\
            positions than `ntol`, establish a link.
        rtol (`float`): if two trajectories have more numbers of overlapped\
            positions, choose the one with smaller reprojection error.

    Return:
        `list` of (`numpy.ndarray`, `float`): trajectories without\
            spatial overlap
    """
    trajs_filtered = []
    for t in trajectories:
        not_nan = np.logical_not(np.isnan(t[0].T[0]))
        if np.sum(not_nan) > 1:
            trajs_filtered.append(t)
    if len(trajs_filtered) == 0:
        return []
    # join overlapped trajectories
    op = get_overlap_pairs(trajs_filtered, ntol, rtol)
    jop = join_pairs(op)
    if len(jop) == 0:
        return trajs_filtered
    else:
        trajs_opt = []
        not_unique = np.hstack(jop).ravel()
        unique = np.setdiff1d(
            np.arange(len(trajs_filtered)), not_unique
        )
        for i in unique:
            trajs_opt.append(trajs_filtered[i])

        for p in jop:
            best_idx = np.argmin([trajs_filtered[idx][1] for idx in p])
            chosen_traj = trajs_filtered[p[best_idx]]
            trajs_opt.append(chosen_traj)
        return trajs_opt


def get_temporal_overlapped_pairs(
        batch_1, batch_2, lag, ntol, rtol, unique='conn'
):
    """
    Get pairs that link overlapped trajectories from two batches.
    The temporal size of trajectories in both batches should be the same.

    Args:
        batch_1
            (`list` [ ( `numpy.ndarray`, `float` ) ]):\
            each trajectory is (positions, error), time is `[t0, t0 + size]`
        batch_2
            (`list` [ ( `numpy.ndarray`, `float` ) ]):\
            each trajectory is (positions, error), time is\
            `[t0 + lag, t0 + size + lag]`
        lag (`int`): The temporal lag between two batches
        ntol (`int`): if two trajectories have more numbers of\
            overlapped positions than `ntol`, establish a link
        rtol (`float`): positions whose distance is smaller than `rtol`\
            are considered to be overlapped.

    Return:
        `numpy.ndarray`: indices of a pair of overlapped trajectories,\
            shape (n, 2)
    """
    rtol_sq = rtol ** 2
    dist_mat = np.empty((len(batch_1), len(batch_2), lag))  # (N1, N2, lag)
    for i, t1 in enumerate(batch_1):
        for j, t2 in enumerate(batch_2):
            dist_sq = np.sum(
                np.power(t1[0][-lag:] - t2[0][:lag], 2), axis=1
            )  # (lag,)
            dist_mat[i, j, :] = dist_sq
    with np.errstate(invalid='ignore'):  # ignore the case like NAN < 5
        conn_mat = np.sum(dist_mat < rtol_sq, axis=2)
        adj_mat = conn_mat >= ntol  # (N1, N2)
    if unique and dist_mat.shape[0] > 0 and dist_mat.shape[1] > 0:
        if unique == 'dist':
            min_dist_mat = np.nanmin(dist_mat, axis=2)  # (N1, N2)
            mask_1 = np.isclose(
                min_dist_mat, np.nanmin(min_dist_mat, axis=0)[np.newaxis, :]
            )
            mask_2 = np.isclose(
                min_dist_mat, np.nanmin(min_dist_mat, axis=1)[:, np.newaxis]
            )
        elif unique == 'conn':
            mask_1 = np.isclose(
                conn_mat, np.nanmax(conn_mat, axis=0)[np.newaxis, :]
            )
            mask_2 = np.isclose(
                conn_mat, np.nanmax(conn_mat, axis=1)[:, np.newaxis]
            )
        adj_mat *= np.logical_and(mask_1, mask_2)
    pairs = np.nonzero(adj_mat)
    return np.array(pairs).T  # (N, 2)


def resolve_temporal_overlap(trajectory_batches, lag, ntol, rtol):
    """
    For trajectorie in many batches, extend them if they were overlapped.

    For instance

    .. code-block:: none

        INPUT:
                 | lag |
            ───────────▶              (trajectory in batch 1)
                  ───────────▶        (trajectory in batch 2)
                        ───────────▶  (trajectory in batch 3)
        OUTPUT:
            ───────────────────────▶  (trajectory in result)

    Args:
        trajectory_batches
            (`list` [ `list` [ (`numpy.ndarray`, `float`) ] ]):
            trajectories in different batches
        lag (`int`): the overlapped time between trajectories in two\
            successive batches.  TODO: fix the bug when lag is odd number
        ntol (`int`): if two trajectories have more numbers of\
            overlapped positions than `ntol`, merge the two.
        rtol (`float`): positions whose distance is smaller than `rtol`\
            are considered to be overlapped.

    Return:
        `list` [ (`numpy.ndarray`, `float`) ]: a collection of resolved\
            trajectories
    """
    extended, fragments = [], []
    t0_extended, t0_fragments = [], []
    previous_pairs = np.empty((0, 2), dtype=int)

    for i, b1 in enumerate(trajectory_batches[1:]):

        new_extended = []
        new_t0_extended = []

        b0 = trajectory_batches[i]   # the previous batch, shape (n_traj, ...)
        pairs_extend = get_temporal_overlapped_pairs(extended, b1, lag, ntol, rtol)

        ne_pe_indices = []
        if len(extended) > 0:
            # extended previously extended trajectories,
            new_extended += [
                (
                    np.concatenate(
                        (extended[pe][0][:-lag], b1[p1][0]), axis=0
                    ),
                    extended[pe][1] + b1[p1][1]
                ) for pe, p1 in pairs_extend
            ]
            new_t0_extended += [t0_extended[pe] for pe, p1 in pairs_extend]

            # add non-extended previously extended trajectories into fragments
            ne_pe_indices = np.setdiff1d(
                np.arange(len(extended)), pairs_extend.T[0]
            )
            fragments    += [extended[idx]    for idx in ne_pe_indices]
            t0_fragments += [t0_extended[idx] for idx in ne_pe_indices]

        pne_indices = np.setdiff1d(  # pne -- previously not extended
            np.arange(len(b0)), previous_pairs.T[1]
        )
        pne_trajs = [b0[ni] for ni in pne_indices]

        # trajectories in current batch, but not matched to
        # extended trajectories 
        indice_not_extended = np.setdiff1d(
            np.arange(len(b1)), pairs_extend.T[1]
        )
        b1_not_extended = [b1[idx] for idx in indice_not_extended]

        pairs_new = get_temporal_overlapped_pairs(
            pne_trajs, b1_not_extended, lag, ntol, rtol
        )
        remap = np.arange(len(b1))[indice_not_extended]
        pairs_new[:, 1] = remap[pairs_new[:, 1]]  # remap to the indices of b1

        # extended previously not extended trajectories
        new_extended += [
            (
                np.concatenate((pne_trajs[pn][0][:-lag], b1[p1][0]), axis=0),
                pne_trajs[pn][1] + b1[p1][1]  # reprojection error
            ) for pn, p1 in pairs_new
        ]
        new_t0_extended += [i * lag  for _ in pairs_new]

        # add non-extended previously non-extended trajectories into fragments
        ne_ne_indices = np.setdiff1d(np.arange(len(pne_trajs)), pairs_new.T[0])
        fragments    += [pne_trajs[idx] for idx in ne_ne_indices]
        t0_fragments += [i * lag        for _   in ne_ne_indices]

        previous_pairs = np.vstack(  # (n1, 2) + (n2, 2) -> (n1+n2, 2)
            (pairs_extend, pairs_new)
        )
        t0_extended = new_t0_extended
        extended = new_extended

    # For the last batch
    b0 = trajectory_batches[-2]   # the previous batch, shape (n_traj, ...)
    pne_indices = np.setdiff1d(  # pne -- previously not extended
        np.arange(len(b0)),
        previous_pairs.T[1]
    )
    pne_trajs = [b0[ni] for ni in pne_indices]
    fragments += pne_trajs
    t0_fragments += [(len(trajectory_batches)-1) * lag for _ in pne_trajs]

    return extended + fragments, t0_extended + t0_fragments


def get_trajectory_batches(
        cameras, features_mv_mt, st_error_tol, search_range, tau,
        z_min, z_max, overlap_num, overlap_rtol, reproj_err_tol, t1=1
    ):
    """
    Getting short 3D trajectories batches from 2D positions and camera informations
    A batch is trajectories start from `t0` to `t0 + tau`
    It is designed so that the same object will overlap in different batches, and
    they can be joined with function :any:`resolve_temporal_overlap`

    .. code-block:: none

        ───────────▶              (trajectory in batch 1)
              ───────────▶        (trajectory in batch 2)
                    ───────────▶  (trajectory in batch 3)

    Args:
        cameras (Camera): cameras for 3 views
        freatrues_mv_mt (:obj:`list`): 2d features in different views at different frames
        st_error_tol (:obj:`float`): the stereo reprojection error cut-off for stereo linking
        tau (:obj:`int`): the length of trajectories in each batch; unit: frame
            the overlap between different batches will be `tau // 2`
        z_min (:obj:`float`): the minimum allowed z-positions for all trajectories
        z_max (:obj:`float`): the maximum allowed z-positions for all trajectories
        overlap_num (:obj:`int`): if two trajectories have more numbers of
            overlapped positions than `overlap_num`, establish a link.
        overlap_rtol (:obj:`float`): if two trajectories have more numbers of
            overlapped positions, link them.
        reproj_err_tol (:obj:`float`): the 3d positiosn whose reprojection error
            is greater than this will not be re-constructed, instead a NAN
            is inserted into the trajectory

    Return:
        :obj:`list` [ :obj:`list` [ (:obj:`numpy.ndarray`, :obj:`float`) ] ]:
            trajectories in different batches
    """
    shift = tau // 2
    frame_num = len(features_mv_mt)
    t_starts = [t * shift for t in range(frame_num // shift)]
    t_starts = [t for t in t_starts if t + tau <= frame_num]
    proj_mats = [cam.p for cam in cameras]
    cam_origins = [cam.o for cam in cameras]
    batches = []
    for i, t0 in enumerate(t_starts):
        print(f"processing batch {i}")
        stereo_matches = []
        for features_mv in features_mv_mt[t0 : t0 + tau]:
            matched = match_v3(
                *features_mv, *proj_mats, *cam_origins,
                tol_2d=st_error_tol, optimise=True
            )
            stereo_matches.append(matched)
        features_mt_mv = []  # shape (3, frames, n, 3)
        for view in range(3):
            features_mt_mv.append([])
            for frame in range(t0, t0 + tau):
                features_mt_mv[-1].append( features_mv_mt[frame][view] )
        if t1 == 1:
            ctrajs_3d = get_trajs_3d(
                features_mt_mv, stereo_matches, proj_mats, cam_origins,
                c_max=500,
                search_range=search_range, re_max=reproj_err_tol
            )
        else:
            ctrajs_3d = get_trajs_3d_t1t2(
                features_mt_mv, stereo_matches, proj_mats, cam_origins,
                c_max=500,
                search_range=search_range,
                search_range_traj=search_range,
                tau_1=t1, tau_2=tau//t1,
                re_max=reproj_err_tol
            )
        ctrajs_3d = get_valid_ctraj(ctrajs_3d, z_min, z_max)
        ctrajs_3d = remove_spatial_overlap(ctrajs_3d, overlap_num, overlap_rtol)
        batches.append(ctrajs_3d)
    return batches


def get_brcs(number=1, bias=(1.0, 0.7, 0.8), brightness=(0.25, 1.0)):
    """
    Get biased random colours

    Args:
        number (int): the number of random colours.
        bias (tuple): the bias towards different base colours, in the format
            of (r, g, b). The value of r/g/b should range between 0 and 1.
        brightness (tuple): the brightness in the format of (low, high), the
            value of low/high should range from 0 to 1.

    Return:
        np.ndarray: the colors, shape (n, 3) or (3) if number==1
    """
    if number > 1:
        rgbs = np.random.uniform(brightness[0], brightness[1], (number, 3))
    else:
        rgbs = np.random.uniform(brightness[0], brightness[1], 3)
    colors = rgbs * np.array(bias)
    return colors


def draw_fish(positions, ax, size=1):
    """
    Draw fish shaped scatter on a matplotlib Axes object

    Args:
        positions (np.ndarray): positions of the 3D points, shape (n, 3)
        ax (Axes): an Axes instance from matplotlib
        size (float): the size of the fish

    Return:
        None
    """
    fish_vertices = np.array([
        (0, 0), (1, 2), (5, 2),
        (7, 0), (8.5, -1.5), (8.4, 0), (8.5, 1.5), (7, 0),
        (5, -2.2), (1, -2.5), (0, 0)
    ]) / 10 * size
    codes = [1, 4, 4, 4, 2, 2, 2, 2, 4, 4, 4]
    for p in positions:
        shift = np.array([p[d] for d in range(3) if d != 1])[np.newaxis, :]
        v = fish_vertices + shift
        fish = PathPatch(
            Path(v, codes=codes),
            facecolor=get_brcs(1),
            edgecolor='k'
        )
        ax.add_patch(fish)
        art3d.pathpatch_2d_to_3d(
            fish, z=p[1], zdir="y",
        )


def plot_cameras(ax, cameras, water_level=0, depth=400):
    """
    Draw cameras on the matplotlib Axes instance with water.

    This is typically designed to represent the experiment in my PhD.

    Args:
        ax (Axes): an Axes instance from matplotlib
        cameras (Camera): the instance of Camera class
        water_level (float): the water level in the world coordinate
        depth (float): the depth of the water

    Return:
        Axes: the axes with the cameras
    """
    origins = []
    camera_size = 100
    focal_len = 2
    ray_length = 500
    camera_segments = [
            np.array(([0, 0, 0], [1, -1, focal_len])) * camera_size,
            np.array(([0, 0, 0], [-1, 1, focal_len])) * camera_size,
            np.array(([0, 0, 0], [-1, -1, focal_len])) * camera_size,
            np.array(([0, 0, 0], [1, 1, focal_len])) * camera_size,
            np.array(([1, 1, focal_len], [1, -1, focal_len])) * camera_size,
            np.array(([1, -1, focal_len], [-1, -1, focal_len])) * camera_size,
            np.array(([-1, -1, focal_len], [-1, 1, focal_len])) * camera_size,
            np.array(([-1, 1, focal_len], [1, 1, focal_len])) * camera_size
            ]
    for cam in cameras:
        origin = -cam.r.T @ cam.t
        orient = cam.r.T @ np.array([0, 0, 1])
        origins.append(origin)
        for seg in camera_segments:
            to_plot = np.array([cam.r.T @ p + origin for p in seg])
            ax.plot(*to_plot.T, color='deeppink')
        ax.quiver(*origin, *orient * ray_length, color='deeppink')
    for o in origins:
        ax.scatter(*o, color='w', edgecolor='deeppink', zorder=6)

    xlim, ylim, zlim = np.array(origins).T
    mid_x = np.mean(xlim)
    mid_y = np.mean(ylim)
    x = np.linspace(mid_x - 1e3, mid_x + 1e3, 11, endpoint=True)
    y = np.linspace(mid_y - 1e3, mid_y + 1e3, 11, endpoint=True)
    x, y = np.meshgrid(x, y)
    ax.plot_surface(x, y, np.ones(x.shape) * water_level, alpha=0.3)
    ax.set_zlim(-depth, 2000)
    return ax


def solve_overlap_lp(points, errors, diameter):
    """
    Remove overlapped particles using linear programming, following

        1. minimize the total error
        2. particles do not overlap
        3. retain most particles

    Args:
        points (np.ndaray): particle locations, shape (N, dimension)
        errors (np.ndarray): the error (cost) of each particle, shape (N, )
        diameter (float): the minimium distance between non-overlap particles

    Return:
        np.ndarray: the optimised positions, shape (N', dimension)
    """
    N, dim = points.shape
    dists = pdist(points)
    if np.all(dists > diameter):  # do not optimise if no overlap
        return points
    dist_mat = squareform(dists)  # (N, N)
    if N <= 1:
        return points
    for n_target in range(N, 1, -1):
        model = Model(name="Overlap Model")
        x_vars = [model.binary_var(name=f"x_{i}") for i in range(N)]
        # total number == n_target
        model.add_constraint(model.sum(x_vars) == n_target)
        # no overlap
        for i, j in product(np.arange(N), repeat=2):
            if i != j:
                model.add_constraint(
                    (dist_mat[i, j] - diameter) * x_vars[i] * x_vars[j] >= 0
                )
        objective = model.sum(x * e for x, e in zip(x_vars, errors))
        model.minimize(objective)
        is_successful = model.solve()
        if is_successful:
            indices = np.array([
                x.solution_value for x in x_vars
            ], dtype=bool)
            return points[indices]
    return points[np.argmin(errors)][np.newaxis, :]


def __solve_overlap_lp_phys(points, errors, diameter, beta, mu):
    """
    Remove overlapped particles using linear programming, following

        1. minimize the total error and energy
        2. particles do not overlap, with a soft boundary
        3. retain most particles

    Args:
        points (np.ndaray): particle locations, shape (N, dimension)
        errors (np.ndarray): the error (cost) of each particle, shape (N, )
        diameter (float): the minimium distance between non-overlap particles
        C (float): the inverse temperature
        mu (float): the chemical potential

    Return:
        np.ndarray: the optimised positions, shape (N', dimension)
    """
    N, dim = points.shape
    dists = pdist(points)
    errors = errors - mu
    if np.all(dists > diameter):  # do not optimise if no overlap
        return points
    dist_mat = squareform(dists)  # (N, N)
    if N <= 1:
        return points

    model = Model(name="Overlap Model")
    x_vars = [model.binary_var(name=f"x_{i}") for i in range(N)]
    slack_vars = model.continuous_var_matrix(
        keys1=np.arange(N),
        keys2=np.arange(N),
        lb=0, ub=diameter, name="slack"
    )
    for i, j in product(np.arange(N), repeat=2):
        if i != j:
            model.add_constraint(
                (dist_mat[i, j] - diameter) * x_vars[i] * x_vars[j] \
                >=  - slack_vars[i, j]
            )
    energy = model.sum(slack_vars) * beta
    objective = model.sum(x * e for x, e in zip(x_vars, errors))
    model.minimize(objective + energy)
    is_successful = model.solve()
    if is_successful:
        indices = np.array([
            x.solution_value for x in x_vars
        ], dtype=bool)
        return points[indices]
    else:
        return points[np.argmin(errors)][np.newaxis, :]


def solve_overlap_lp_fast(points, errors, diameter):
    """
    Remove overlapped particles using linear programming, following

        1. minimize the total error
        2. particles do not overlap
        3. retain most particles

    Args:
        points (np.ndaray): particle locations, shape (N, dimension)
        errors (np.ndarray): the error (cost) of each particle, shape (N, )
        diameter (float): the minimium distance between non-overlap particles

    Return:
        np.ndarray: the optimised positions, shape (N', dimension)
    """
    N, dim = points.shape
    dists = pdist(points)
    aij = squareform(dists) < diameter
    np.fill_diagonal(aij, 0)
    n_cliques, labels = connected_components(aij, directed=False)
    result = []
    for val in range(n_cliques):
        clique = np.nonzero(labels == val)[0]
        if len(clique) == 1:
            result.append(points[clique])
        else:
            result.append(
                solve_overlap_lp(
                    points[clique], errors[clique], diameter=diameter
                )
            )
    return np.concatenate(result, axis=0)


def solve_overlap_lp_phys(points, errors, diameter, beta, mu):
    """
    Remove overlapped particles using linear programming, following

        1. minimize the total error
        2. particles do not overlap
        3. retain most particles

    Args:
        points (np.ndaray): particle locations, shape (N, dimension)
        errors (np.ndarray): the error (cost) of each particle, shape (N, )
        diameter (float): the minimium distance between non-overlap particles

    Return:
        np.ndarray: the optimised positions, shape (N', dimension)
    """
    N, dim = points.shape
    dists = pdist(points)
    aij = squareform(dists) < diameter
    np.fill_diagonal(aij, 0)
    n_cliques, labels = connected_components(aij, directed=False)
    result = []
    for val in range(n_cliques):
        clique = np.nonzero(labels == val)[0]
        if len(clique) == 1:
            result.append(points[clique])
        else:
            result.append(
                __solve_overlap_lp_phys(
                    points[clique], errors[clique], diameter=diameter,
                    beta=beta, mu=mu
                )
            )
    return np.concatenate(result, axis=0)


def refine_trajectory(trajectory, cameras, features, tol_2d):
    """
    Refine the trajectory so that each reprojected position
        matches the features detected in different cameras.

    The purpose of the function is to optimise the linearly
        interpolated trajectories.

    Args:
        trajectory (numpy.ndarray): a fully interpolated trajectory,
            shape (n_frame, 3).
        cameras (list): a collections of cameras.
        features (list): the positions of 2D features from
            different cameras, shape of element: (n_frame, 2).
        tol_2d (float): the tolerance for 2D reprojection errors.
            The very problematic 3D points will be replaced by the
            origional points in the `trajectory`.

    Return:
        numpy.ndarray: a optimised trajectory, where 3D locations
            are re-calculated from the 2D features.
    """
    n_frame = len(trajectory)
    n_view = len(cameras)

    traj_reproj = np.array([
        cam.project_refractive(trajectory) for cam in cameras
    ])
    traj_2d_nview = np.empty((n_view, n_frame, 2))

    mask = np.ones(n_frame).astype(bool)


    for f in range(n_frame):
        for v in range(n_view):
            reproj = traj_reproj[v][f]
            distances = np.linalg.norm(features[v][f] - reproj, axis=1)
            nearest = np.argmin(distances)
            if distances[nearest] > tol_2d:
                mask[f] = False
            traj_2d_nview[v][f] = features[v][f][nearest]

    undist = np.array([
        cameras[v].undistort_points(
            traj_2d[mask], want_uv=True
        ) for v, traj_2d in enumerate(traj_2d_nview)
    ])

    refined = refractive_triangulate(
        *undist,
        *[cam.p for cam in cameras],
        *[cam.o for cam in cameras],
    )

    new_traj = trajectory.copy()
    new_traj[mask] = refined

    return new_traj


def optimise_c2c(R, T, K, C, p2d, p3dh, method='Nelder-Mead'):
    """
    Optimise the camera extrinsic parameters by measuring the ellipse (conic)\
    projected by a circle at plane :math:`\\pi_z = (0, 0, 1, 0)^T`.

    Args:
        R (numpy.ndarray): the rotation vector :math:`\\in so(3)`
        T (numpy.ndarray): the translation vector :math:`\\in \\mathbb{R}^3`
        K (numpy.ndarray): the calibration matrix
            :math:`\\in \\mathbb{R}^{3 \\times 3}`
        C (numpy.ndarray): the matrix representation of a conic,
            shape (3, 3). The conic is a projection of the circle.
        p2d (numpy.ndarray): the *undistorted* 2d points for the PnP problem,\
            shape (n, 2)
        p3dh (numpy.ndarray): the homogeneous representations of 3d points\
            for the PnP problem, shape (n, 3)

    Return:
        tuple: the optimised :math:`\\mathbf{R} \\in so(3)` and\
        :math:`\\mathbf{T} \\in \\mathbb{R}^3`
    """
    result = minimize(
        fun=cost_conic, x0=np.concatenate((R, T)),
        args=(K, C, p2d, p3dh),
        method=method
    )
    return result.x[:3], result.x[3:]


def optimise_triplet_c2c(
    R1, T1, R2, T2, R3, T3, K1, K2, K3, C1, C2, C3, p2d1, p2d2, p2d3, p3dh,
    force_circle
):
    """
    Optimise three camera extrinsic parameters by measuring the ellipse (conic)\
    projected by a circle at plane :math:`\\pi_z = (0, 0, 1, 0)^T`.

    Args:
        R1 (numpy.ndarray): the rotation vector :math:`\\in so(3)` of camera 1
        T1 (numpy.ndarray): the translation vector :math:`\\in \\mathbb{R}^3`\
            of camera 1
        R2 (numpy.ndarray): the rotation vector :math:`\\in so(3)` of camera 2
        T2 (numpy.ndarray): the translation vector :math:`\\in \\mathbb{R}^3`\
            of camera 2
        R3 (numpy.ndarray): the rotation vector :math:`\\in so(3)` of camera 3
        T3 (numpy.ndarray): the translation vector :math:`\\in \\mathbb{R}^3`\
            of camera 3
        K1 (numpy.ndarray): the calibration matrix\
            :math:`\\in \\mathbb{R}^{3 \\times 3}` of camera 1
        K2 (numpy.ndarray): the calibration matrix\
            :math:`\\in \\mathbb{R}^{3 \\times 3}` of camera 2
        K3 (numpy.ndarray): the calibration matrix\
            :math:`\\in \\mathbb{R}^{3 \\times 3}` of camera 3
        C1 (numpy.ndarray): the conic matrix from camera 1, shape (3, 3)
        C2 (numpy.ndarray): the conic matrix from camera 2, shape (3, 3)
        C3 (numpy.ndarray): the conic matrix from camera 3, shape (3, 3)
        p2d1 (numpy.ndarray): the *undistorted* 2d features for the PnP\
            problem from camera 1, shape (n, 2)
        p2d2 (numpy.ndarray): the *undistorted* 2d features for the PnP\
            problem from camera 2, shape (n, 2)
        p2d3 (numpy.ndarray): the *undistorted* 2d features for the PnP\
            problem from camera 3, shape (n, 2)
        p3dh (numpy.ndarray): the homogeneous representations of 3d points\
            for the PnP problem, shape (n, 3)
        force_circle (bool): if True, the cost function will force the\
            reconstructed conic to be a circle.

    Return:
        tuple: the optimised :math:`\\mathbf{R} \\in so(3)` and\
        :math:`\\mathbf{T} \\in \\mathbb{R}^3`

    """
    RT123 = np.concatenate((R1, T1, R2, T2, R3, T3))
    if force_circle:
        result = minimize(
            fun=cost_circle_triple, x0=RT123,
            args=(K1, K2, K3, C1, C2, C3, p2d1, p2d2, p2d3, p3dh)
        )
    else:
        result = minimize(
            fun=cost_conic_triple, x0=RT123,
            args=(K1, K2, K3, C1, C2, C3, p2d1, p2d2, p2d3, p3dh)
        )
    opt = result.x
    return opt.reshape((6, 3), order="C")


def get_optimised_camera_c2c(
    camera, conic_mat, p2d, p3d, method='Nelder-Mead'
):
    """
    Optimise the extrinsic parameter of the camera with a known 3D circle.

    Args:
        camera (Camera): a calibrated camera, its extrinsic parameters will
            be used as the initial guess for the optimisation.
        conic_mat (numpy.ndarray): the matrix representation of an ellipse.
            the parameter should be obtained from an undistorted image.
        p2d (numpy.ndarray): the *undistorted* 2d locations for the PnP problem, shape (n, 2)
        p3d (numpy.ndarray): the 3d locations for the PnP problem, shape (n, 3)
        method (str): the name of the optimisation mathod. See scipy doc.

    Return:
        Camera: a new camera with better extrinsic parameters.
    """
    R = camera.rotation.as_rotvec()
    T = camera.t
    K = camera.k
    p3dh = np.concatenate((p3d, np.ones((len(p3d), 1))), axis=1)
    opt = optimise_c2c(
        R, T, K, camera.undistort_points(p2d), p3dh, conic_mat, method
    )
    return get_updated_camera(camera, *opt)


def get_optimised_camera_triplet(cameras, p2ds, p3d):
    """
    Optimise 3 cameras with 2D-3D correspondances

    Args:
        cameras (list): three :obj:`Camera` instances
        p2ds (list): three *distorted* 2d locations whose shape is (n, 2)
        p3d (numpy.ndarray): the 3d locations, shape (n, 3)

    Return:
        list: three cameras whose extrinsic parameters were optimised
            with the circle-to-conic correspondances.
    """
    p3dh = np.concatenate((p3d, np.ones((p3d.shape[0], 1))), axis=1)
    R_opt, T_opt = [], []
    for i, cam in enumerate(cameras):
        _, R, T = cv2.solvePnP(
            objectPoints=p3d[None, :, :],
            imagePoints=p2ds[i],
            cameraMatrix=cam.k,
            distCoeffs=cam.distortion,
            flags=cv2.SOLVEPNP_ITERATIVE
        )
        R = R.ravel()
        T = T.ravel()
        R_opt.append(R)
        T_opt.append(T)

    return [
        get_updated_camera(c, r, t) for c, r, t in zip(cameras, R_opt, T_opt)
    ]


def get_optimised_camera_triplet_c2c(
    cameras, conic_matrices, p2ds, p3d, force_circle, method="Nelder-Mead"
):
    """
    Optimise 3 cameras with 2D-3D correspondances as well as a measured
        conics corresponding to a 3D circle on the plane :math:`Z=0`.

    Args:
        cameras (list): three :obj:`Camera` instances
        conic_matrices(list): three conic matrices with shape (3, 3)
        p2ds (list): three *distorted* 2d locations whose shape is (n, 2)
        p3d (numpy.ndarray): the 3d locations, shape (n, 3)
        force_circle (bool): if True the cost function will force the\
            reconstructed ellipse to be a circle.
        method (str): the method for the non-lienar optimisation

    Return:
        list: three cameras whose extrinsic parameters were optimised
            with the circle-to-conic correspondances.
    """
    p3dh = np.concatenate((p3d, np.ones((p3d.shape[0], 1))), axis=1)
    RT, K = [], []
    for i, cam in enumerate(cameras):
        _, R, T = cv2.solvePnP(
            objectPoints=p3d[None, :, :],
            imagePoints=p2ds[i],
            cameraMatrix=cam.k,
            distCoeffs=cam.distortion,
            flags=cv2.SOLVEPNP_ITERATIVE
        )
        if force_circle:
            R, T =optimise_c2c(
                R, T, K=cam.k, C=conic_matrices[i],
                p2d=cam.undistort_points(p2ds[i]),
                p3dh=p3dh,
            )
        else:
            R = R.ravel()
            T = T.ravel()
        RT.append(R)
        RT.append(T)
        K.append(cam.k)

    r1, t1, r2, t2, r3, t3 = optimise_triplet_c2c(
        *RT, *K, *conic_matrices,
        *[c.undistort_points(p) for c, p in zip(cameras, p2ds)],
        p3dh, force_circle
    )

    R_opt = [r1, r2, r3]
    T_opt = [t1, t2, t3]
    return [
        get_updated_camera(c, r, t) for c, r, t in zip(cameras, R_opt, T_opt)
    ]


def get_cost_camera_triplet_c2c(cameras, conic_matrices, p2ds, p3d):
    """
    Optimise 3 cameras with 2D-3D correspondances as well as a measured
        conics corresponding to a 3D circle on the plane :math:`Z=0`.

    Args:
        cameras (list): three :obj:`Camera` instances
        conic_matrices(list): three conic matrices with shape (3, 3)
        p2ds (list): three *distorted* 2d locations whose shape is (n, 2)
        p3d (numpy.ndarray): the 3d locations, shape (n, 3)

    Return:
        float: the reprojected geometrical error.
    """
    p3dh = np.concatenate((p3d, np.ones((p3d.shape[0], 1))), axis=1)
    RT, K = [], []
    for i, cam in enumerate(cameras):
        RT.append(Rotation.from_matrix(cam.r).as_rotvec())
        RT.append(cam.t)
        K.append(cam.k)
    RT = np.concatenate(RT)
    return cost_conic_triple(RT, *K, *conic_matrices, *p2ds, p3dh)


def get_updated_camera(camera, R, T):
    """
    Get the updated camera with new rotation and translation.

    Args:
        R (numpy.ndarray): the new rotation vector :math:`\\in so(3)`
        T (numpy.ndarray): the new translation vector\
            :math:`\\in \\mathbb{R}^3`

    Return:
        Camera: a new `Camera` instance whose extrinsic parameters\
            were updated.
    """
    new_cam = Camera()
    new_cam.k = camera.k.copy()
    new_cam.distortion = camera.distortion.copy()
    new_cam.rotation = Rotation.from_rotvec(R.ravel())
    new_cam.t = T.ravel().copy()
    new_cam.update()
    return new_cam


def get_relative_euclidean_transform(Rotations, Translations, index):
    """
    Calculate the averaged *relative* rotation and translation from\
        different noisy measurements. There is an unknown Euclidean\
        ambiguity between the different measurements.

    Args:
        Rotations (np.ndarray): shape (n_measure, n_view, 3, 3)
        Translations (np.ndarray): shape (n_measure, n_view, 3)
        index (int): the transforms are bettwen different views\
            and the view specified by the index

    Return:
        tuple: the relative rotations and translations. The shape of\
            the elements are ((n_view, 3, 3), (n_view, 3))
    """
    n_measure, n_view = Rotations.shape[:2]
    R0 = Rotations[:, index, :, :]  # (n_measure, 3, 3)
    T0 = Translations[:, index, :]  # (n_measure, 3,)
    Rij = np.einsum('nmik,njk->nmij', Rotations, R0)  # R = Ri @ R0^T
    Tij = Translations - np.einsum('nmik,nk->nmi', Rij, T0)  # T = Ti - R @ T0
    Rij = np.mean(Rij, axis=0)  # (n_view, 3, 3)
    Tij = np.mean(Tij, axis=0)  # (n_view, 3,)
    return Rij, Tij


def get_cameras_with_averaged_euclidean_transform(cameras, index):
    """
    Update the extrinsic parameters of the cameras so that the relative\
        rotation and translation between different views were replace by\
        the average of different measurements.

    Args:
        cameras (list): a list of cameras, with "shape" (n_measure, n_view).
        index (int): the transforms are bettwen different views\
            and the view specified by the index

    Return:
        list: a list of cameras whose relative Euclidean transformations were\
            replaced by the average over different measurements. The "shape"\
            of the final result is `(n_measure, n_view)`.
    """
    n_measure = len(cameras)
    n_view = len(cameras[0])
    Rotations = np.empty((n_measure, n_view, 3, 3))
    Translations = np.empty((n_measure, n_view, 3))
    for i in range(n_measure):
        for j in range(n_view):
            Rotations[i, j, :, :] = cameras[i][j].r
            Translations[i, j, :] = cameras[i][j].t
    Rij, Tij = get_relative_euclidean_transform(Rotations, Translations, index)
    R0, T0 = Rotations[:, index, :, :], Translations[:, index, :]
    Ri = np.einsum('mik,nkj->nmij', Rij, R0)  # (n_measure, n_view, 3, 3)
    Ti = np.einsum('mik,nk->nmi', Rij, T0) + Tij  # (n_measure, n_view, 3)
    cameras_updated = []
    for i in range(n_measure):
        ensemble = []
        for j in range(n_view):
            r = Rotation.from_matrix(Ri[i, j]).as_rotvec()
            t = Ti[i, j]
            ensemble.append(get_updated_camera(cameras[i][j], r, t))
        cameras_updated.append(ensemble)
    return cameras_updated


def remove_camera_triplet_outliers(camera_triplets, threshold):
    """
    Remove camera triplets whose relative orientation/translation are very\
        different.

    Args:
        camera_triplets (list): a collection of tuples with 3 cameras
        threshold (float): if the delta value (delta = [x - mean] / std)\
            is greater than the threshold, the camera triplet is considered\
            to be an outlier.

    Return:
        list: a collection of tuples with 3 cameras, where outlier triplets\
            were discarded.
    """
    Rotations = np.array([[c.r for c in trip] for trip in camera_triplets])
    Translations = np.array([[c.t for c in trip] for trip in camera_triplets])
    R0 = Rotations[:, 0, :, :]  # (n_measure, 3, 3)
    T0 = Translations[:, 0, :]  # (n_measure, 3,)
    Rij = np.einsum('nmik,njk->nmij', Rotations, R0)  # R = Ri @ R0^T
    Tij = Translations - np.einsum('nmik,nk->nmi', Rij, T0)  # T = Ti - R @ T0

    R12 = Rotation.from_matrix(Rij[:, 1]).as_rotvec()
    R13 = Rotation.from_matrix(Rij[:, 2]).as_rotvec()
    Z = np.concatenate((R12, R13, Tij[:, 1], Tij[:, 2]), axis=1)
    Z = np.abs((Z - Z.mean(axis=0)[None, :]) / Z.std(axis=0)[None, :])
    result = []
    for ct, z in zip(camera_triplets, Z.max(axis=1)):
        if z < threshold:
            result.append(ct)
    return result
