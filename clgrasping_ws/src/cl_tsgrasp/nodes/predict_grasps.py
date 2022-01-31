#! /home/tim/anaconda3/envs/tsgrasp/bin/python
# shebang is for the Python3 environment with all dependencies

# tsgrasp dependencies
import sys
from typing import List
sys.path.append("/home/tim/Research/cl_grasping/clgrasping_ws/src/cl_tsgrasp/")
from nn.load_model import load_model

# ROS dependencies
import rospy
from geometry_msgs.msg import Pose, PoseStamped
from std_msgs.msg import Header
from sensor_msgs.msg import PointCloud2, PointField
from rospy.numpy_msg import numpy_msg
from cl_tsgrasp.msg import Grasps
import sensor_msgs.point_cloud2 as pcl2
import ros_numpy

# python dependencies
import numpy as np
from collections import deque
import torch
from threading import Lock
import copy
from kornia.geometry.conversions import quaternion_to_rotation_matrix, rotation_matrix_to_quaternion, QuaternionCoeffOrder
import math
import MinkowskiEngine as ME
from pytorch3d.ops import sample_farthest_points
# torch.backends.cudnn.benchmark=True # makes a big difference on FPS for some PTS_PER_FRAME values, but seems to increase memory usage and can result in OOM errors.

## global constants
QUEUE_LEN       = 4
PTS_PER_FRAME   = 45000
GRIPPER_DEPTH   = 0.1034
CONF_THRESHOLD  = 0 #0.5
TOP_K           = 400
# BOUNDS          = torch.Tensor([[-0.1, -0.3, 0.3], [0.1, -0.1, 0.65]]) # (xyz_lower, xyz_upper)

BOUNDS          = torch.Tensor([[-0.2, -0.3, 0.3], [0.3, 0.3, 0.65]]) # (xyz_lower, xyz_upper)

TF_ROLL, TF_PITCH, TF_YAW = 0, 0, math.pi/2
TF_X, TF_Y, TF_Z = 0, 0, 0

## load Pytorch Lightning network
device = torch.device('cuda')
pl_model = load_model().to(device)
pl_model.eval()

## Start node
rospy.init_node('grasp_detection')
## Output publishers
grasp_pub = rospy.Publisher('tsgrasp/grasps', numpy_msg(Grasps), queue_size=10)
pcl_pub = rospy.Publisher("/tsgrasp/confs3d", PointCloud2)

queue = deque(maxlen=QUEUE_LEN) # FIFO queue, right side is most recent
queue_mtx = Lock()
latest_header = Header()

ee_pose_msg = None

def ee_pose_cb(msg):
    global ee_pose_msg
    ee_pose_msg = msg

import time
class TimeIt:
    def __init__(self, s):
        self.s = s
        self.t0 = None
        self.t1 = None
        self.print_output = True

    def __enter__(self):
        self.t0 = time.time()

    def __exit__(self, t, value, traceback):
        # torch.cuda.synchronize()
        self.t1 = time.time()
        if self.print_output:
            print('%s: %s' % (self.s, self.t1 - self.t0))


# https://stackoverflow.com/questions/59387182/construct-a-rotation-matrix-in-pytorch
#@torch.jit.script
def eul_to_rotm(roll: float, pitch: float, yaw: float):
    """Convert euler angles to rotation matrix."""
    roll = torch.tensor([roll])
    pitch = torch.tensor([pitch])
    yaw = torch.tensor([yaw])

    tensor_0 = torch.zeros(1)
    tensor_1 = torch.ones(1)

    RX = torch.stack([
                    torch.stack([tensor_1, tensor_0, tensor_0]),
                    torch.stack([tensor_0, torch.cos(roll), -torch.sin(roll)]),
                    torch.stack([tensor_0, torch.sin(roll), torch.cos(roll)])]).reshape(3,3)

    RY = torch.stack([
                    torch.stack([torch.cos(pitch), tensor_0, torch.sin(pitch)]),
                    torch.stack([tensor_0, tensor_1, tensor_0]),
                    torch.stack([-torch.sin(pitch), tensor_0, torch.cos(pitch)])]).reshape(3,3)

    RZ = torch.stack([
                    torch.stack([torch.cos(yaw), -torch.sin(yaw), tensor_0]),
                    torch.stack([torch.sin(yaw), torch.cos(yaw), tensor_0]),
                    torch.stack([tensor_0, tensor_0, tensor_1])]).reshape(3,3)

    R = torch.mm(RZ, RY)
    R = torch.mm(R, RX)
    return R

#@torch.jit.script
def inverse_homo(tf):
    """Compute inverse of homogeneous transformation matrix.

    The matrix should have entries
    [[R,       Rt]
     [0, 0, 0, 1]].
    """
    R = tf[0:3, 0:3]
    t = R.T @ tf[0:3, 3].reshape(3, 1)
    return torch.cat([
        torch.cat([R.T, -t], dim=1),
        torch.tensor([[0, 0, 0, 1]]).to(R.device)
        ],dim=0
    )

def transform_to_eq_pose(poses):
    """Apply the static frame transformation between the network output and the 
    input expected by the servoing logic at /panda/cartesian_impendance_controller/equilibrium_pose.
    
    The servoing pose is at the gripper pads, and is rotated, while the network output is at the wrist.
    
    This is an *intrinsic* pose transformation, where each grasp pose moves a fixed amount relative to 
    its initial pose, so we right-multiply instead of left-multiply."""

    roll, pitch, yaw = TF_ROLL, TF_PITCH, TF_YAW
    x, y, z = TF_X, TF_Y, TF_Z
    tf = torch.cat([
        torch.cat([eul_to_rotm(roll, pitch, yaw), torch.Tensor([x, y, z]).reshape(3, 1)], dim=1),
        torch.Tensor([0, 0, 0, 1]).reshape(1, 4)
    ], dim=0).to(poses.device)
    return poses @ tf

def transform_vec(x: torch.Tensor, tf: torch.Tensor) -> torch.Tensor:
    """Transform 3D vector `x` by homogenous transformation `tf`.

    Args:
        x (torch.Tensor): (b, ..., 3) coordinates in R3
        tf (torch.Tensor): (b, 4, 4) homogeneous pose matrix

    Returns:
        torch.Tensor: (b, ..., 3) coordinates of transformed vectors.
    """

    x_dim = len(x.shape)
    assert all((
        len(tf.shape)==3,           # tf must be (b, 4, 4)
        tf.shape[1:]==(4, 4),       # tf must be (b, 4, 4)
        tf.shape[0]==x.shape[0],    # batch dimension must be same
        x_dim > 2                   # x must be a batched matrix/tensor
    )), "Argument shapes are unsupported."

    x_homog = torch.cat(
        [x, torch.ones(*x.shape[:-1], 1, device=x.device)], 
        dim=-1
    )
    
    # Pad the dimension of tf for broadcasting.
    # E.g., if x had shape (2, 3, 7, 3), and tf had shape (2, 4, 4), then
    # we reshape tf to (2, 1, 1, 4, 4)
    tf = tf.reshape(tf.shape[0], *([1]*(x_dim-3)), 4, 4)

    return (x_homog @ tf.transpose(-2, -1))[..., :3]

#@torch.jit.script
def build_6dof_grasps(contact_pts, baseline_dir, approach_dir, grasp_width, gripper_depth: float=GRIPPER_DEPTH):
    """Calculate the SE(3) transforms corresponding to each predicted coord/approach/baseline/grasp_width grasp.

    Unbatched for torch.jit.script.

    Args:
        contact_pts (torch.Tensor): (N, 3) contact points predicted
        baseline_dir (torch.Tensor): (N, 3) gripper baseline directions
        approach_dir (torch.Tensor): (N, 3) gripper approach directions
        grasp_width (torch.Tensor): (N, 3) gripper width

    Returns:
        pred_grasp_tfs (torch.Tensor): (N, 4, 4) homogeneous grasp poses.
    """
    N = contact_pts.shape[0]
    grasps_R = torch.stack([baseline_dir, torch.cross(approach_dir, baseline_dir), approach_dir], dim=-1)
    grasps_t = contact_pts + grasp_width/2 * baseline_dir - gripper_depth * approach_dir
    ones = torch.ones((N, 1, 1), device=contact_pts.device)
    zeros = torch.zeros((N, 1, 3), device=contact_pts.device)
    homog_vec = torch.cat([zeros, ones], dim=-1)

    pred_grasp_tfs = torch.cat([
        torch.cat([grasps_R, grasps_t.unsqueeze(-1)], dim=-1), 
        homog_vec
    ], dim=-2)
    return pred_grasp_tfs

#@torch.jit.script
def discretize(positions: torch.Tensor, grid_size: float) -> torch.Tensor:
    """Truncate each position to an integer grid."""
    return (positions / grid_size).int()

#@torch.jit.script
def prepend_coordinate(matrix: torch.Tensor, coord: int):
        """Concatenate a constant column of value `coord` before a 2D matrix."""
        return torch.column_stack([
            coord * torch.ones((len(matrix), 1), device=matrix.device),
            matrix
        ])

def unweighted_sum(coords: torch.Tensor):
    """Create a feature vector from a coordinate array, so each 
    row's feature is the number of rows that share that coordinate."""
    
    unique_coords, idcs, counts = coords.unique(dim=0, return_counts=True, return_inverse=True)
    features = counts[idcs]
    return features.reshape(-1, 1).to(torch.float32)

def infer_grasps(tsgraspnet, points: List[torch.Tensor], grid_size: float) -> torch.Tensor:
    """Run a sparse convolutional network on a list of consecutive point clouds, and return the grasp predictions for the last point cloud. Each point cloud may have different numbers of points."""

    ## Convert list of point clouds into matrix of 4D coordinate
    coords = list(map(
        lambda mtx_coo: prepend_coordinate(*mtx_coo),
        zip(points, range(len(points)))
    ))
    coords = torch.cat(coords, dim=0)
    coords = prepend_coordinate(coords, 0) # add dummy batch dimension

    ## Discretize coordinates to integer grid
    coords = discretize(coords, grid_size).contiguous()
    feats = unweighted_sum(coords)

    ## Construct a Minkoswki sparse tensor and run forward inference
    stensor = ME.SparseTensor(
        coordinates = coords,
        features = feats
    )
    print(coords.shape)

    with TimeIt("   tsgraspnet.model.forward"):
        class_logits, baseline_dir, approach_dir, grasp_offset = tsgraspnet.model.forward(stensor)

    ## Return the grasp predictions for the latest point cloud
    idcs = coords[:,1] == coords[:,1].max()
    return(
        class_logits[idcs], baseline_dir[idcs], approach_dir[idcs], grasp_offset[idcs], points[-1]
    )

#@torch.jit.script
def in_bounds(world_pts, BOUNDS):
    """Remove any points that are out of bounds"""
    x, y, z = world_pts[..., 0], world_pts[..., 1], world_pts[..., 2]
    in_bounds = (
        (x > BOUNDS[0][0]) * 
        (y > BOUNDS[0][1]) * 
        (z > BOUNDS[0][2]) * 
        (x < BOUNDS[1][0]) *
        (y < BOUNDS[1][1]) * 
        (z < BOUNDS[1][2] )
    )
    return in_bounds

def bound_point_cloud(pts, poses):
    for i, pose in zip(range(len(pts)), poses):
        world_pc = transform_vec(
            pts[i].unsqueeze(0), pose.unsqueeze(0)
        )[0]
        valid = in_bounds(world_pc, BOUNDS)
        pts[i] = pts[i][valid]
    
    ## ensure nonzero
    if sum(len(pt) for pt in pts) == 0:
        print("No points in bounds")
        return

    return pts

#@torch.jit.script
def downsample_xyz(pts: List[torch.Tensor], pts_per_frame: int) -> List[torch.Tensor]:
    ## downsample point clouds proportion of points -- will that result in same sampling distribution?
    for i in range(len(pts)):
        pts_to_keep = int(pts_per_frame / 90_000 * len(pts[i]))
        idxs = torch.randperm(
            len(pts[i]), dtype=torch.int32, device=pts[i].device
        )[:pts_to_keep].sort()[0].long()

        pts[i] = pts[i][idxs]
    
    return pts

def transform_to_camera_frame(pts, poses):
    ## Transform all point clouds into the frame of the most recent image
    tf_from_cam_i_to_cam_N = inverse_homo(poses[-1]) @ poses
    pts =[
        transform_vec(
            pts[i].unsqueeze(0), 
            tf_from_cam_i_to_cam_N[i].unsqueeze(0)
        )[0]
        for i in range(len(pts))
    ]
    return pts

def identify_grasps(pts):
    outputs = infer_grasps(pl_model, pts, grid_size=pl_model.model.grid_size)

    class_logits, baseline_dir, approach_dir, grasp_offset, positions = outputs

    # only retain the information from the latest (-1) frame
    grasps = build_6dof_grasps(positions, baseline_dir, approach_dir, grasp_offset)
    grasps = transform_to_eq_pose(grasps)
    confs = torch.sigmoid(class_logits)

    return grasps, confs

def filter_grasps(grasps, confs):

    try: 
        len(confs.squeeze())
    except:
        breakpoint()

    # confidence thresholding
    grasps = grasps[confs.squeeze() > CONF_THRESHOLD]
    confs = confs.squeeze()[confs.squeeze() > CONF_THRESHOLD]

    if grasps.shape[0] == 0 or confs.shape[0] == 0:
        return None, None

    # top-k selection
    vals, top_idcs = torch.topk(confs.squeeze(), k=min(TOP_K, len(confs.squeeze())), sorted=True)
    grasps = grasps[top_idcs]
    confs = confs[top_idcs]

    if grasps.shape[0] == 0:
        return None, None

    # furthest point sampling
    # # furthest point sampling by position
    pos = grasps[:,:3,3]
    _, selected_idcs = sample_farthest_points(pos.unsqueeze(0), K=TOP_K)
    selected_idcs = selected_idcs.squeeze()

    # grasps = grasps[selected_idcs]
    # confs = confs[selected_idcs]

    return grasps, confs

@torch.inference_mode()
def find_grasps():
    global queue, device, queue_mtx

    with TimeIt("FIND_GRASPS() fn: "):
        with TimeIt('Unpack pointclouds'):
            # only copy the queue with the mutex. Afterward, process the copy.
            with queue_mtx:
                if len(queue) != QUEUE_LEN: return
                q = copy.deepcopy(queue)
                # queue.clear()
                queue.popleft()

            try:
                msgs, poses = list(zip(*q))
                with TimeIt("   ros_numpy: "):
                    pts = [
                            ros_numpy.point_cloud2.pointcloud2_to_xyz_array(msg, remove_nans=False).reshape(-1,3)
                        for msg in msgs]

                # device = "cpu"
                with TimeIt("   pts_to_gpu: "):
                    pts = [torch.from_numpy(pt.astype(np.float32)).to(device) for pt in pts]
                poses = torch.Tensor(np.stack(poses)).to(device, non_blocking=True)
            except ValueError as e:
                print(e)
                print("Is this error because there are fewer than 300x300 points?")
                return

            header = msgs[-1].header

        ## Processing pipeline

        # Start with pts, a list of Torch point clouds.
        # Remove points that are outside of the boundaries in the global frame.
        with TimeIt('Bound Point Cloud'):
            pts             = bound_point_cloud(pts, poses)
            if pts is None: return

        # Downsample the points with uniform probability.
        with TimeIt('Downsample Points'):
            pts             = downsample_xyz(pts, PTS_PER_FRAME)
            if pts is None: return

        # Transform the points into the frame of the last camera perspective.
        with TimeIt('Transform to Camera Frame'):
            pts             = transform_to_camera_frame(pts, poses)
            if pts is None or len(pts[-1]) == 0: return

        # Run the NN to identify grasp poses and confidences.
        with TimeIt('Find Grasps'):
            grasps, confs   = identify_grasps(pts)
            all_confs       = confs.clone() # keep the pointwise confs for plotting later
            if pts is None or len(pts[-1]) == 0: return

        # Filter the grasps by thresholding and furthest-point sampling.
        with TimeIt('Filter Grasps'):
            grasps, confs   = filter_grasps(grasps, confs)

        if grasps is None: return
        
        with TimeIt('Publish Grasps'):
        
            # convert homogeneous poses to ros message poses
            qs = rotation_matrix_to_quaternion(grasps[:,:3,:3].contiguous(), order = QuaternionCoeffOrder.XYZW).cpu().numpy()
            vs = grasps[:,:3,3].cpu().numpy()

            def q_v_to_pose(q, v):
                p = Pose()
                p.position.x, p.position.y, p.position.z = v
                (
                    p.orientation.x, p.orientation.y, p.orientation.z, p.orientation.w 
                ) = q
                return p

            grasps_msg = Grasps()
            grasps_msg.poses = [q_v_to_pose(q, v) for q, v in zip(qs, vs)]
            grasps_msg.confs = confs
            grasps_msg.header = copy.copy(header)
            grasp_pub.publish(grasps_msg)

        with TimeIt('Publish Colored Point Cloud'):
            #  https://gist.github.com/lucasw/ea04dcd65bc944daea07612314d114bb
            # (N x 4) array, with fourth item as alpha
            cloud_points = pts[-1]
            downsample = 1
            cloud_points = torch.cat([
                cloud_points[::downsample], 
                all_confs[::downsample]], 
                dim=1
            ).cpu().numpy()
            fields = [PointField('x', 0, PointField.FLOAT32, 1),
                PointField('y', 4, PointField.FLOAT32, 1),
                PointField('z', 8, PointField.FLOAT32, 1),
                PointField('alpha', 12, PointField.FLOAT32, 1),
            ]

            scaled_polygon_pcl = pcl2.create_cloud(header, fields, cloud_points)
            pcl_pub.publish(scaled_polygon_pcl)

# https://robotics.stackexchange.com/questions/20069/are-rospy-subscriber-callbacks-executed-sequentially-for-a-single-topic
# https://nu-msr.github.io/me495_site/lecture08_threads.html
def depth_callback(depth_msg):
    global queue, queue_mtx, ee_pose_msg
    # print(f'Message received: {depth_msg.header.stamp}')
    # with TimeIt('Queueing'):

    ee_tf = torch.eye(4)
    ee_orn = ee_pose_msg.pose.orientation
    ee_orn = torch.Tensor([ee_orn.x, ee_orn.y, ee_orn.z, ee_orn.w])
    ee_orn = quaternion_to_rotation_matrix(ee_orn, order=QuaternionCoeffOrder.XYZW)
    ee_tf[:3, :3] = ee_orn

    ee_pos = ee_pose_msg.pose.position
    ee_pos = torch.Tensor([ee_pos.x, ee_pos.y, ee_pos.z])
    ee_tf[:3, 3] = ee_pos 
    
    with queue_mtx:
        queue.append((depth_msg, ee_tf))

# subscribe to throttled point cloud
# depth_sub = rospy.Subscriber('/tsgrasp/points', PointCloud2, depth_callback, queue_size=1)
depth_sub = rospy.Subscriber('/camera/depth/points', PointCloud2, depth_callback, queue_size=1)
ee_pose = rospy.Subscriber('/tsgrasp/cam_pose', PoseStamped, ee_pose_cb, queue_size=1)
while not rospy.is_shutdown():
    print("##########################################################")
    find_grasps()