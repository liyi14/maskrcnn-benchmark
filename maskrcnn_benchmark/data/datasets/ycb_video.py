import torch
import torch.utils.data as data

import os, math
import sys
import os.path as osp
from os.path import *
import numpy as np
import numpy.random as npr
import cv2
import pickle as cPickle
import scipy.io
import copy
import glob

from maskrcnn_benchmark.structures.bounding_box import BoxList
from maskrcnn_benchmark.structures.segmentation_mask import SegmentationMask
from maskrcnn_benchmark.utils.blob import pad_im, chromatic_transform, add_noise, add_noise_cuda
from maskrcnn_benchmark.utils.se3 import *
from maskrcnn_benchmark.utils.pose_error import *
from transforms3d.quaternions import mat2quat, quat2mat
from maskrcnn_benchmark.ycb_render.ycb_renderer import YCBRenderer

def VOCap(rec, prec):
    index = np.where(np.isfinite(rec))[0]
    rec = rec[index]
    prec = prec[index]
    if len(rec) == 0 or len(prec) == 0:
        ap = 0
    else:
        mrec = np.insert(rec, 0, 0)
        mrec = np.append(mrec, 0.1)
        mpre = np.insert(prec, 0, 0)
        mpre = np.append(mpre, prec[-1])
        for i in range(1, len(mpre)):
            mpre[i] = max(mpre[i], mpre[i-1])
        i = np.where(mrec[1:] != mrec[:-1])[0] + 1
        ap = np.sum(np.multiply(mrec[i] - mrec[i-1], mpre[i])) * 10
    return ap

class YCBVideoDataset(data.Dataset):
    def __init__(self, cfg, image_set, data_dir, transforms=None):

        self.cfg = cfg
        self._name = 'ycb_video_' + image_set
        self._image_set = image_set
        self._ycb_video_path = data_dir
        if self.cfg.DATA_PATH == '' or self.cfg.MODE == 'TEST':
            self._data_path = os.path.join(self._ycb_video_path, 'data')
        else:
            self._data_path = self.cfg.DATA_PATH

        # define all the classes
        self._classes_all = ('__background__', '002_master_chef_can', '003_cracker_box', '004_sugar_box', '005_tomato_soup_can', '006_mustard_bottle', \
                         '007_tuna_fish_can', '008_pudding_box', '009_gelatin_box', '010_potted_meat_can', '011_banana', '019_pitcher_base', \
                         '021_bleach_cleanser', '024_bowl', '025_mug', '035_power_drill', '036_wood_block', '037_scissors', '040_large_marker', \
                         '051_large_clamp', '052_extra_large_clamp', '061_foam_brick')
        self._num_classes_all = len(self._classes_all)
        self._class_colors_all = [(255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255), (0, 255, 255), \
                              (128, 0, 0), (0, 128, 0), (0, 0, 128), (128, 128, 0), (128, 0, 128), (0, 128, 128), \
                              (64, 0, 0), (0, 64, 0), (0, 0, 64), (64, 64, 0), (64, 0, 64), (0, 64, 64), 
                              (192, 0, 0), (0, 192, 0),	 (0, 0, 192)]
        self._symmetry_all = np.array([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]).astype(np.float32)
        self._extents_all = self._load_object_extents()

        self._width = 640
        self._height = 480
        self._intrinsic_matrix = np.array([[1.066778e+03, 0.000000e+00, 3.129869e+02],
                                          [0.000000e+00, 1.067487e+03, 2.413109e+02],
                                          [0.000000e+00, 0.000000e+00, 1.000000e+00]])

        # select a subset of classes
        self._classes = [self._classes_all[i] for i in self.cfg.TRAIN.CLASSES]
        self._num_classes = len(self._classes)
        self._class_colors = [self._class_colors_all[i] for i in self.cfg.TRAIN.CLASSES]
        self._symmetry = self._symmetry_all[list(self.cfg.TRAIN.CLASSES)]
        self._extents = self._extents_all[list(self.cfg.TRAIN.CLASSES)]
        self._points, self._points_all, self._point_blob, self._points_clamp = self._load_object_points()
        self._PIXEL_MEANS = np.array([[[102.9801, 115.9465, 122.7717]]])
        self._pixel_mean = torch.tensor(self._PIXEL_MEANS / 255.0).cuda().float()

        self._classes_other = []
        for i in range(self._num_classes_all):
            if i not in self.cfg.TRAIN.CLASSES:
                # do not use clamp
                if i == 19 and 20 in self.cfg.TRAIN.CLASSES:
                    continue
                if i == 20 and 19 in self.cfg.TRAIN.CLASSES:
                    continue
                self._classes_other.append(i)
        self._num_classes_other = len(self._classes_other)

        # 3D model paths
        self.model_mesh_paths = ['{}/models/{}/textured_simple.obj'.format(self._ycb_video_path, cls) for cls in self._classes_all[1:]]
        self.model_texture_paths = ['{}/models/{}/texture_map.png'.format(self._ycb_video_path, cls) for cls in self._classes_all[1:]]
        self.model_colors = [np.array(self._class_colors_all[i]) / 255.0 for i in range(1, len(self._classes_all))]

        self.model_mesh_paths_target = ['{}/models/{}/textured_simple.obj'.format(self._ycb_video_path, cls) for cls in self._classes[1:]]
        self.model_texture_paths_target = ['{}/models/{}/texture_map.png'.format(self._ycb_video_path, cls) for cls in self._classes[1:]]
        self.model_colors_target = [np.array(self._class_colors_all[i]) / 255.0 for i in self.cfg.TRAIN.CLASSES[1:]]

        self._class_to_ind = dict(zip(self._classes, range(self._num_classes)))
        self._image_ext = '.png'
        self._image_index = self._load_image_set_index(image_set)

        if (self.cfg.MODE == 'TRAIN' and self.cfg.TRAIN.SYNTHESIZE) or (self.cfg.MODE == 'TEST' and self.cfg.TEST.SYNTHESIZE):
            self._size = len(self._image_index) * (self.cfg.TRAIN.SYN_RATIO+1)
        else:
            self._size = len(self._image_index)
        if self._size > self.cfg.TRAIN.MAX_ITERS_PER_EPOCH * self.cfg.SOLVER.IMS_PER_BATCH:
            self._size = self.cfg.TRAIN.MAX_ITERS_PER_EPOCH * self.cfg.SOLVER.IMS_PER_BATCH
        self._roidb = self.gt_roidb()
        if self.cfg.MODE == 'TRAIN' or self.cfg.TEST.VISUALIZE:
            self._perm = np.random.permutation(np.arange(len(self._roidb)))
        else:
            self._perm = np.arange(len(self._roidb))
        self._cur = 0
        if self.cfg.MODE == 'TRAIN' or (self.cfg.MODE == 'TEST' and self.cfg.TEST.SYNTHESIZE == True):
            self._build_background_images()
        self._build_uniform_poses()

        # poses from the dataset
        if self.cfg.MODE == 'TRAIN' or (self.cfg.MODE == 'TEST' and self.cfg.TEST.SYNTHESIZE == True):
            self._poses = self._load_all_poses()
            self._pose_indexes = np.zeros((self._num_classes-1, ), dtype=np.int32)

        assert os.path.exists(self._ycb_video_path), \
                'ycb_video path does not exist: {}'.format(self._ycb_video_path)
        assert os.path.exists(self._data_path), \
                'Data path does not exist: {}'.format(self._data_path)

        print('loading 3D models')
        self.renderer = YCBRenderer(width=self.cfg.TRAIN.SYN_WIDTH, height=self.cfg.TRAIN.SYN_HEIGHT, render_marker=False)
        self.renderer.load_objects(self.model_mesh_paths, self.model_texture_paths, self.model_colors)
        self.renderer.set_camera_default()
        print(self.model_mesh_paths)


    @property
    def name(self):
        return self._name

    @property
    def num_classes(self):
        return len(self._classes)

    @property
    def classes(self):
        return self._classes

    @property
    def class_colors(self):
        return self._class_colors

    @property
    def cache_path(self):
        cache_path = osp.abspath(osp.join(self._ycb_video_path, '..', 'cache'))
        if not os.path.exists(cache_path):
            os.makedirs(cache_path)
        return cache_path


    # backproject pixels into 3D points in camera's coordinate system
    def backproject(self, depth_cv, intrinsic_matrix, factor):

        depth = depth_cv.astype(np.float32, copy=True) / factor

        index = np.where(~np.isfinite(depth))
        depth[index[0], index[1]] = 0

        # get intrinsic matrix
        K = intrinsic_matrix
        Kinv = np.linalg.inv(K)

        # compute the 3D points
        width = depth.shape[1]
        height = depth.shape[0]

        # construct the 2D points matrix
        x, y = np.meshgrid(np.arange(width), np.arange(height))
        ones = np.ones((height, width), dtype=np.float32)
        x2d = np.stack((x, y, ones), axis=2).reshape(width*height, 3)

        # backprojection
        R = np.dot(Kinv, x2d.transpose())

        # compute the 3D points
        X = np.multiply(np.tile(depth.reshape(1, width*height), (3, 1)), R)
        return np.array(X).transpose().reshape((height, width, 3))


    def _build_uniform_poses(self):

        self.eulers = []
        interval = self.cfg.TRAIN.UNIFORM_POSE_INTERVAL
        for yaw in range(-180, 180, interval):
            for pitch in range(-90, 90, interval):
                for roll in range(-180, 180, interval):
                    self.eulers.append([yaw, pitch, roll])

        # sample indexes
        num_poses = len(self.eulers)
        num_classes = len(self._classes_all) - 1 # no background
        self.pose_indexes = np.zeros((num_classes, ), dtype=np.int32)
        self.pose_lists = []
        for i in range(num_classes):
            self.pose_lists.append(np.random.permutation(np.arange(num_poses)))


    def _build_background_images(self):

        backgrounds_color = []
        backgrounds_depth = []
        if self.cfg.TRAIN.SYN_BACKGROUND_SPECIFIC:
            # NVIDIA
            allencenter = os.path.join(self.cache_path, '../AllenCenter/data')
            subdirs = os.listdir(allencenter)
            for i in range(len(subdirs)):
                subdir = subdirs[i]
                files = os.listdir(os.path.join(allencenter, subdir))
                for j in range(len(files)):
                    filename = os.path.join(allencenter, subdir, files[j])
                    backgrounds_color.append(filename)
        else:
            '''
            # SUN 2012
            root = os.path.join(self.cache_path, '../SUN2012/data/Images')
            subdirs = os.listdir(root)

            for i in range(len(subdirs)):
                subdir = subdirs[i]
                names = os.listdir(os.path.join(root, subdir))

                for j in range(len(names)):
                    name = names[j]
                    if os.path.isdir(os.path.join(root, subdir, name)):
                        files = os.listdir(os.path.join(root, subdir, name))
                        for k in range(len(files)):
                            if os.path.isdir(os.path.join(root, subdir, name, files[k])):
                                filenames = os.listdir(os.path.join(root, subdir, name, files[k]))
                                for l in range(len(filenames)):
                                    filename = os.path.join(root, subdir, name, files[k], filenames[l])
                                    backgrounds.append(filename)
                            else:
                                filename = os.path.join(root, subdir, name, files[k])
                                backgrounds.append(filename)
                    else:
                        filename = os.path.join(root, subdir, name)
                        backgrounds.append(filename)

            # ObjectNet3D
            objectnet3d = os.path.join(self.cache_path, '../ObjectNet3D/data')
            files = os.listdir(objectnet3d)
            for i in range(len(files)):
                filename = os.path.join(objectnet3d, files[i])
                backgrounds.append(filename)
            '''

            # PASCAL 2012
            pascal = os.path.join(self.cache_path, '../PASCAL2012/data')
            files = os.listdir(pascal)
            for i in range(len(files)):
                filename = os.path.join(pascal, files[i])
                backgrounds_color.append(filename)

            '''
            # YCB Background
            ycb = os.path.join(self.cache_path, '../YCB_Background')
            files = os.listdir(ycb)
            for i in range(len(files)):
                filename = os.path.join(ycb, files[i])
                backgrounds.append(filename)
            '''

        # depth background
        kinect = os.path.join(self.cache_path, '../Kinect')
        subdirs = os.listdir(kinect)
        for i in range(len(subdirs)):
            subdir = subdirs[i]
            files = glob.glob(os.path.join(self.cache_path, '../Kinect', subdir, '*depth*'))
            for j in range(len(files)):
                filename = os.path.join(self.cache_path, '../Kinect', subdir, files[j])
                backgrounds_depth.append(filename)

        for i in range(len(backgrounds_color)):
            if not os.path.isfile(backgrounds_color[i]):
                print('file not exist {}'.format(backgrounds_color[i]))

        for i in range(len(backgrounds_depth)):
            if not os.path.isfile(backgrounds_depth[i]):
                print('file not exist {}'.format(backgrounds_depth[i]))

        self._backgrounds_color = backgrounds_color
        self._backgrounds_depth = backgrounds_depth
        print('build color background images finished, {:d} images'.format(len(backgrounds_color)))
        print('build depth background images finished, {:d} images'.format(len(backgrounds_depth)))


    def _render_item(self):

        height = self.cfg.TRAIN.SYN_HEIGHT
        width = self.cfg.TRAIN.SYN_WIDTH
        fx = self._intrinsic_matrix[0, 0]
        fy = self._intrinsic_matrix[1, 1]
        px = self._intrinsic_matrix[0, 2]
        py = self._intrinsic_matrix[1, 2]
        zfar = 6.0
        znear = 0.01
        classes = np.array(self.cfg.TRAIN.CLASSES)

        # sample target objects
        if self.cfg.TRAIN.SYN_SAMPLE_OBJECT:
            maxnum = np.minimum(self.num_classes-1, self.cfg.TRAIN.SYN_MAX_OBJECT)
            num = np.random.randint(self.cfg.TRAIN.SYN_MIN_OBJECT, maxnum+1)
            perm = np.random.permutation(np.arange(self.num_classes-1))
            indexes_target = perm[:num] + 1
        else:
            num = self.num_classes - 1
            indexes_target = np.arange(num) + 1
        num_target = num
        cls_indexes = [self.cfg.TRAIN.CLASSES[i]-1 for i in indexes_target]

        # sample other objects as distractors
        num_other = min(5, self._num_classes_other)
        perm = np.random.permutation(np.arange(self._num_classes_other))
        indexes = perm[:num_other]
        for i in range(num_other):
            cls_indexes.append(self._classes_other[indexes[i]]-1)

        # sample poses
        num = num_target + num_other
        poses_all = []
        for i in range(num):
            qt = np.zeros((7, ), dtype=np.float32)
            # rotation
            cls = int(cls_indexes[i])

            if self.cfg.TRAIN.SYN_SAMPLE_POSE and i < num_target:
                # sample from dataset poses
                cls_ind = np.where(classes == cls+1)[0]
                cls_ind = int(cls_ind) - 1
                if self._pose_indexes[cls_ind] >= len(self._poses[cls_ind]):
                    self._pose_indexes[cls_ind] = 0
                    pindex = np.random.permutation(np.arange(len(self._poses[cls_ind])))
                    self._poses[cls_ind] = self._poses[cls_ind][pindex]
                ind = self._pose_indexes[cls_ind]
                pose = self._poses[cls_ind][ind, :]
                euler = pose[:3] + (self.cfg.TRAIN.SYN_STD_ROTATION * math.pi / 180.0) * np.random.randn(3)
                qt[3:] = euler2quat(euler[0], euler[1], euler[2])
                self._pose_indexes[cls_ind] += 1

                qt[0] = pose[3] + np.random.uniform(-0.1, 0.1)
                qt[1] = pose[4] + np.random.uniform(-0.1, 0.1)
                qt[2] = pose[5] + np.random.uniform(-0.1, 0.1)

            else:
                # uniformly sample poses
                if self.pose_indexes[cls] >= len(self.pose_lists[cls]):
                    self.pose_indexes[cls] = 0
                    self.pose_lists[cls] = np.random.permutation(np.arange(len(self.eulers)))
                yaw = self.eulers[self.pose_lists[cls][self.pose_indexes[cls]]][0] + 15 * np.random.randn()
                pitch = self.eulers[self.pose_lists[cls][self.pose_indexes[cls]]][1] + 15 * np.random.randn()
                roll = self.eulers[self.pose_lists[cls][self.pose_indexes[cls]]][2] + 15 * np.random.randn()
                qt[3:] = euler2quat(yaw * math.pi / 180.0, pitch * math.pi / 180.0, roll * math.pi / 180.0, 'syxz')
                self.pose_indexes[cls] += 1

                # translation
                bound = self.cfg.TRAIN.SYN_BOUND
                if i == 0 or i >= num_target or np.random.rand(1) > 0.5:
                    qt[0] = np.random.uniform(-bound, bound)
                    qt[1] = np.random.uniform(-bound, bound)
                    qt[2] = np.random.uniform(self.cfg.TRAIN.SYN_TNEAR, self.cfg.TRAIN.SYN_TFAR)
                else:
                    # sample an object nearby
                    object_id = np.random.randint(0, i, size=1)[0]
                    extent = np.mean(self._extents_all[cls+1, :])

                    flag = np.random.randint(0, 2)
                    if flag == 0:
                        flag = -1
                    qt[0] = poses_all[object_id][0] + flag * extent * np.random.uniform(1.0, 1.5)
                    if np.absolute(qt[0]) > bound:
                        qt[0] = poses_all[object_id][0] - flag * extent * np.random.uniform(1.0, 1.5)
                    if np.absolute(qt[0]) > bound:
                        qt[0] = np.random.uniform(-bound, bound)

                    flag = np.random.randint(0, 2)
                    if flag == 0:
                        flag = -1
                    qt[1] = poses_all[object_id][1] + flag * extent * np.random.uniform(1.0, 1.5)
                    if np.absolute(qt[1]) > bound:
                        qt[1] = poses_all[object_id][1] - flag * extent * np.random.uniform(1.0, 1.5)
                    if np.absolute(qt[1]) > bound:
                        qt[1] = np.random.uniform(-bound, bound)

                    qt[2] = poses_all[object_id][2] - extent * np.random.uniform(2.0, 4.0)
                    if qt[2] < self.cfg.TRAIN.SYN_TNEAR:
                        qt[2] = poses_all[object_id][2] + extent * np.random.uniform(2.0, 4.0)

            poses_all.append(qt)
        self.renderer.set_poses(poses_all)

        # sample lighting
        # light pose
        theta = np.random.uniform(-np.pi/2, np.pi/2)
        phi = np.random.uniform(0, np.pi/2)
        r = np.random.uniform(0.25, 3.0)
        light_pos = [r * np.sin(theta) * np.sin(phi), r * np.cos(phi) + np.random.uniform(-2, 2), r * np.cos(theta) * np.sin(phi)]
        self.renderer.set_light_pos(light_pos)

        # light color
        intensity = np.random.uniform(0.5, 3.0)
        light_color = intensity * np.random.uniform(0.5, 1.5, 3)
        self.renderer.set_light_color(light_color)
            
        # rendering
        self.renderer.set_projection_matrix(width, height, fx, fy, px, py, znear, zfar)
        image_tensor = torch.cuda.FloatTensor(height, width, 4).detach()
        seg_tensor = torch.cuda.FloatTensor(height, width, 4).detach()
        self.renderer.render(cls_indexes, image_tensor, seg_tensor)
        image_tensor = image_tensor.flip(0)
        seg_tensor = seg_tensor.flip(0)

        # foreground mask
        seg = seg_tensor[:,:,2] + 256*seg_tensor[:,:,1] + 256*256*seg_tensor[:,:,0]
        seg_input = seg.clone().cpu().numpy()

        # RGB to BGR order
        im = image_tensor.cpu().numpy()
        im = np.clip(im, 0, 1)
        im = im[:, :, (2, 1, 0)] * 255
        im = im.astype(np.uint8)

        im_label = seg_tensor.cpu().numpy()
        im_label = im_label[:, :, (2, 1, 0)] * 255
        im_label = np.round(im_label).astype(np.uint8)
        im_label = np.clip(im_label, 0, 255)
        im_label, im_label_all = self.process_label_image(im_label)

        # add background to the image
        ind = np.random.randint(len(self._backgrounds_color), size=1)[0]
        filename = self._backgrounds_color[ind]
        background_color = cv2.imread(filename, cv2.IMREAD_UNCHANGED)

        try:
            # randomly crop a region as background
            bw = background_color.shape[1]
            bh = background_color.shape[0]
            x1 = npr.randint(0, int(bw/3))
            y1 = npr.randint(0, int(bh/3))
            x2 = npr.randint(int(2*bw/3), bw)
            y2 = npr.randint(int(2*bh/3), bh)
            background_color = background_color[y1:y2, x1:x2]
            background_color = cv2.resize(background_color, (self._width, self._height), interpolation=cv2.INTER_LINEAR)
        except:
            background_color = np.zeros((self._height, self._width, 3), dtype=np.uint8)
            print('bad background image')

        if len(background_color.shape) != 3:
            background_color = np.zeros((self._height, self._width, 3), dtype=np.uint8)
            print('bad background image')

        # paste objects on background
        I = np.where(im_label_all == 0)
        im[I[0], I[1], :] = background_color[I[0], I[1], :3]
        im = im.astype(np.uint8)

        # chromatic transform
        if self.cfg.TRAIN.CHROMATIC and self.cfg.MODE == 'TRAIN' and np.random.rand(1) > 0.1:
            im = chromatic_transform(im)

        im_cuda = torch.from_numpy(im).cuda().float() / 255.0
        if self.cfg.TRAIN.ADD_NOISE and self.cfg.MODE == 'TRAIN' and np.random.rand(1) > 0.1:
            im_cuda = add_noise_cuda(im_cuda)
        im_cuda -= self._pixel_mean
        im_cuda = 255.0 * im_cuda.permute(2, 0, 1)
        img = im_cuda.cpu()      

        # boxes, class labels and binary masks
        boxes = []
        class_ids = []
        binary_masks = []
        ratios = []
        for i in range(num_target):
            cls = int(indexes_target[i])
            T = poses_all[i][:3]
            qt = poses_all[i][3:]

            # render to compare mask
            self.renderer.set_poses([poses_all[i]])
            self.renderer.render([self.cfg.TRAIN.CLASSES[cls]-1], image_tensor, seg_tensor)
            seg_tensor = seg_tensor.flip(0)
            seg = seg_tensor[:,:,2] + 256*seg_tensor[:,:,1] + 256*256*seg_tensor[:,:,0]
            seg_target = seg.clone().cpu().numpy()
            non_occluded = np.sum(np.logical_and(seg_target > 0, seg_target == seg_input)).astype(np.float)
            area = np.sum(seg_target > 0).astype(np.float)
            if area > 0:
                occluded_ratio = 1.0 - non_occluded / area
            else:
                occluded_ratio = 1.0
            if occluded_ratio > 0.9:
                continue

            # compute box
            x3d = np.ones((4, self._points_all.shape[1]), dtype=np.float32)
            x3d[0, :] = self._points_all[cls,:,0]
            x3d[1, :] = self._points_all[cls,:,1]
            x3d[2, :] = self._points_all[cls,:,2]
            RT = np.zeros((3, 4), dtype=np.float32)
            RT[:3, :3] = quat2mat(qt)
            RT[:, 3] = T
            x2d = np.matmul(self._intrinsic_matrix, np.matmul(RT, x3d))
            x2d[0, :] = np.divide(x2d[0, :], x2d[2, :])
            x2d[1, :] = np.divide(x2d[1, :], x2d[2, :])
        
            x1 = np.min(x2d[0, :])
            y1 = np.min(x2d[1, :])
            x2 = np.max(x2d[0, :])
            y2 = np.max(x2d[1, :])
            boxes.append([x1, y1, x2, y2])
            class_ids.append(cls)

            # mask
            binary_masks.append((im_label == cls).astype(np.float32))
            ratios.append(occluded_ratio)

        boxes = torch.as_tensor(boxes).reshape(-1, 4)  # guard against no boxes
        target = BoxList(boxes, (self._width, self._height), mode="xyxy")

        class_ids = torch.tensor(class_ids)
        target.add_field("labels", class_ids)

        ratios = torch.tensor(ratios)
        target.add_field("ratios", ratios)

        n = len(binary_masks)
        masks_np = np.zeros((n, self._height, self._width), dtype=np.float32)
        for i in range(n):
            masks_np[i, :, :] = binary_masks[i]

        masks = SegmentationMask(torch.as_tensor(masks_np), (width, height), "mask")
        target.add_field("masks", masks)
        target = target.clip_to_image(remove_empty=True)

        return img, target


    def __getitem__(self, index):

        is_syn = 0
        if ((self.cfg.MODE == 'TRAIN' and self.cfg.TRAIN.SYNTHESIZE) or (self.cfg.MODE == 'TEST' and self.cfg.TEST.SYNTHESIZE)) and (index % (self.cfg.TRAIN.SYN_RATIO+1) != 0):
            is_syn = 1

        if is_syn:
            img, target = self._render_item()
        else:
            if self._cur >= len(self._roidb):
                self._perm = np.random.permutation(np.arange(len(self._roidb)))
                self._cur = 0
            db_ind = self._perm[self._cur]
            roidb = self._roidb[db_ind]
            self._cur += 1

            # Get the input image blob
            random_scale_ind = npr.randint(0, high=len(self.cfg.TRAIN.SCALES_BASE))
            img, im_scale, height, width = self._get_image_blob(roidb, random_scale_ind)

            # build the label blob
            target = self._get_label_blob(roidb, self._num_classes, im_scale, height, width)
            target = target.clip_to_image(remove_empty=True)

        if self.cfg.TRAIN.VISUALIZE:
            self._visualize(img, target)

        return img, target, index


    def _get_image_blob(self, roidb, scale_ind):    

        # rgba
        rgba = pad_im(cv2.imread(roidb['image'], cv2.IMREAD_UNCHANGED), 16)
        if rgba.shape[2] == 4:
            im = np.copy(rgba[:,:,:3])
            alpha = rgba[:,:,3]
            I = np.where(alpha == 0)
            im[I[0], I[1], :] = 0
        else:
            im = rgba

        im_scale = self.cfg.TRAIN.SCALES_BASE[scale_ind]
        if im_scale != 1.0:
            im = cv2.resize(im, None, None, fx=im_scale, fy=im_scale, interpolation=cv2.INTER_LINEAR)
        height = im.shape[0]
        width = im.shape[1]

        if roidb['flipped']:
            im = im[:, ::-1, :]

        # chromatic transform
        if self.cfg.TRAIN.CHROMATIC and self.cfg.MODE == 'TRAIN' and np.random.rand(1) > 0.1:
            im = chromatic_transform(im)

        im_cuda = torch.from_numpy(im).cuda().float() / 255.0
        if self.cfg.TRAIN.ADD_NOISE and self.cfg.MODE == 'TRAIN' and np.random.rand(1) > 0.1:
            im_cuda = add_noise_cuda(im_cuda)
        im_cuda -= self._pixel_mean
        im_cuda = 255.0 * im_cuda.permute(2, 0, 1)
        img = im_cuda.cpu()  

        return img, im_scale, height, width


    def _get_label_blob(self, roidb, num_classes, im_scale, height, width):
        """ build the label blob """

        meta_data = scipy.io.loadmat(roidb['meta_data'])
        meta_data['cls_indexes'] = meta_data['cls_indexes'].flatten()
        classes = np.array(self.cfg.TRAIN.CLASSES)

        # read label image
        im_label = pad_im(cv2.imread(roidb['label'], cv2.IMREAD_UNCHANGED), 16)
        if roidb['flipped']:
            if len(im_label.shape) == 2:
                im_label = im_label[:, ::-1]
            else:
                im_label = im_label[:, ::-1, :]
        if im_scale != 1.0:
            im_label = cv2.resize(im_label, None, None, fx=im_scale, fy=im_scale, interpolation=cv2.INTER_NEAREST)

        # change large clamp to extra large clamp
        im_label_original = im_label.copy()
        I = np.where(im_label == 19)
        im_label[I] = 20

        # foreground mask
        seg = torch.from_numpy((im_label != 0).astype(np.float32))
        mask = seg.unsqueeze(0).repeat((3, 1, 1)).float().cuda()

        # poses
        poses = meta_data['poses']
        if len(poses.shape) == 2:
            poses = np.reshape(poses, (3, 4, 1))
        if roidb['flipped']:
            poses = _flip_poses(poses, meta_data['intrinsic_matrix'], width)

        # boxes, class labels and binary masks
        boxes = []
        class_ids = []
        binary_masks = []
        num = poses.shape[2]
        for i in range(num):
            cls = int(meta_data['cls_indexes'][i])

            # change large clamp to extra large clamp
            if cls == 19:
                clamp = 1
                cls = 20
            else:
                clamp = 0

            ind = np.where(classes == cls)[0]
            if len(ind) > 0:
                R = poses[:, :3, i]
                T = poses[:, 3, i]
                qt = mat2quat(R)

                # compute box
                x3d = np.ones((4, self._points_all.shape[1]), dtype=np.float32)
                if clamp:
                    x3d[0, :] = self._points_clamp[:,0]
                    x3d[1, :] = self._points_clamp[:,1]
                    x3d[2, :] = self._points_clamp[:,2]
                else:
                    x3d[0, :] = self._points_all[ind,:,0]
                    x3d[1, :] = self._points_all[ind,:,1]
                    x3d[2, :] = self._points_all[ind,:,2]
                RT = np.zeros((3, 4), dtype=np.float32)
                RT[:3, :3] = quat2mat(qt)
                RT[:, 3] = T
                x2d = np.matmul(meta_data['intrinsic_matrix'], np.matmul(RT, x3d))
                x2d[0, :] = np.divide(x2d[0, :], x2d[2, :])
                x2d[1, :] = np.divide(x2d[1, :], x2d[2, :])
        
                x1 = np.min(x2d[0, :])
                y1 = np.min(x2d[1, :])
                x2 = np.max(x2d[0, :])
                y2 = np.max(x2d[1, :])
                boxes.append([x1, y1, x2, y2])
                class_ids.append(int(ind))

                x1 = max(int(x1), 0)
                y1 = max(int(y1), 0)
                x2 = min(int(x2), width-1)
                y2 = min(int(y2), height-1)
                labels = np.zeros((height, width), dtype=np.float32)
                labels[y1:y2, x1:x2] = im_label[y1:y2, x1:x2]
                binary_masks.append((labels == cls).astype(np.float32))

        boxes = torch.as_tensor(boxes).reshape(-1, 4)  # guard against no boxes
        target = BoxList(boxes, (width, height), mode="xyxy")

        class_ids = torch.tensor(class_ids)
        target.add_field("labels", class_ids)

        n = len(binary_masks)
        masks_np = np.zeros((n, height, width), dtype=np.float32)
        for i in range(n):
            masks_np[i, :, :] = binary_masks[i]

        masks = SegmentationMask(torch.as_tensor(masks_np), (width, height), "mask")
        target.add_field("masks", masks)

        ratios = np.zeros((n, ), dtype=np.float32)
        ratios = torch.tensor(ratios)
        target.add_field("ratios", ratios)

        return target


    def _visualize(self, img, target):

        boxes = target.bbox.numpy()
        labels = target.extra_fields['labels']
        masks = target.extra_fields['masks'].instances.masks
        ratios = target.extra_fields['ratios'].numpy()

        m = 3
        n = 4

        import matplotlib.pyplot as plt    
        fig = plt.figure()
        start = 1

        # show input image
        im = img.numpy()        
        im = im.transpose((1, 2, 0))
        im += self._PIXEL_MEANS
        im = im[:, :, (2, 1, 0)]
        im = np.clip(im, 0, 255)
        im = im.astype(np.uint8)
        ax = fig.add_subplot(m, n, 1)
        plt.imshow(im)
        ax.set_title('color')
        start += 1

        # show bounding boxes
        ax = fig.add_subplot(m, n, start)
        start += 1
        plt.imshow(im)
        ax.set_title('gt boxes')
        for j in range(boxes.shape[0]):
            x1 = boxes[j, 0]
            y1 = boxes[j, 1]
            x2 = boxes[j, 2]
            y2 = boxes[j, 3]
            cls = int(labels[j])
            color = [self._class_colors[cls][0] / 255.0, self._class_colors[cls][1] / 255.0, self._class_colors[cls][2] / 255.0]
            plt.gca().add_patch(
                plt.Rectangle((x1, y1), x2-x1, y2-y1, fill=False, edgecolor=color, linewidth=3, clip_on=False))

        # show masks
        for i in range(masks.shape[0]):
            mask = masks[i].numpy()
            ax = fig.add_subplot(m, n, start)
            start += 1
            plt.imshow(mask)
            ax.set_title('%.2f'%(ratios[i]))

        plt.show()



    def __len__(self):
        return self._size


    def _load_image_set_index(self, image_set):
        """
        Load the indexes listed in this dataset's image set file.
        """
        image_set_file = os.path.join(self._ycb_video_path, image_set + '.txt')
        assert os.path.exists(image_set_file), \
                'Path does not exist: {}'.format(image_set_file)

        image_index = []
        video_ids_selected = set([])
        video_ids_not = set([])
        count = np.zeros((self.num_classes, ), dtype=np.int32)

        with open(image_set_file) as f:
            for x in f.readlines():
                index = x.rstrip('\n')
                pos = index.find('/')
                video_id = index[:pos]

                if not video_id in video_ids_selected and not video_id in video_ids_not:
                    filename = os.path.join(self._data_path, video_id, '000001-meta.mat')
                    meta_data = scipy.io.loadmat(filename)
                    cls_indexes = meta_data['cls_indexes'].flatten()
                    flag = 0
                    for i in range(len(cls_indexes)):
                        cls_index = int(cls_indexes[i])
                        ind = np.where(np.array(self.cfg.TRAIN.CLASSES) == cls_index)[0]
                        if len(ind) > 0:
                            count[ind] += 1
                            flag = 1
                    if flag:
                        video_ids_selected.add(video_id)
                    else:
                        video_ids_not.add(video_id)

                if video_id in video_ids_selected:
                    image_index.append(index)

        for i in range(1, self.num_classes):
            print('%d %s [%d/%d]' % (i, self.classes[i], count[i], len(list(video_ids_selected))))

        # sample a subset for training
        if image_set == 'train':
            image_index = image_index[::10]

        return image_index


    def _load_object_points(self):

        points = [[] for _ in range(len(self._classes))]
        num = np.inf

        for i in range(1, len(self._classes)):
            point_file = os.path.join(self._ycb_video_path, 'models', self._classes[i], 'points.xyz')
            print(point_file)
            assert os.path.exists(point_file), 'Path does not exist: {}'.format(point_file)
            points[i] = np.loadtxt(point_file)
            if points[i].shape[0] < num:
                num = points[i].shape[0]

        points_all = np.zeros((self._num_classes, num, 3), dtype=np.float32)
        for i in range(1, len(self._classes)):
            points_all[i, :, :] = points[i][:num, :]

        # rescale the points
        point_blob = points_all.copy()
        for i in range(1, self._num_classes):
            # compute the rescaling factor for the points
            weight = 10.0 / np.amax(self._extents[i, :])
            if weight < 10:
                weight = 10
            if self._symmetry[i] > 0:
                point_blob[i, :, :] = 4 * weight * point_blob[i, :, :]
            else:
                point_blob[i, :, :] = weight * point_blob[i, :, :]

        # points of large clamp
        point_file = os.path.join(self._ycb_video_path, 'models', '051_large_clamp', 'points.xyz')
        points_clamp = np.loadtxt(point_file)
        points_clamp = points_clamp[:num, :]

        return points, points_all, point_blob, points_clamp


    def _load_object_extents(self):

        extent_file = os.path.join(self._ycb_video_path, 'extents.txt')
        assert os.path.exists(extent_file), \
                'Path does not exist: {}'.format(extent_file)

        extents = np.zeros((self._num_classes_all, 3), dtype=np.float32)
        extents[1:, :] = np.loadtxt(extent_file)

        return extents


    # image
    def image_path_at(self, i):
        """
        Return the absolute path to image i in the image sequence.
        """
        return self.image_path_from_index(self.image_index[i])

    def image_path_from_index(self, index):
        """
        Construct an image path from the image's "index" identifier.
        """

        image_path = os.path.join(self._data_path, index + '-color' + self._image_ext)
        assert os.path.exists(image_path), \
                'Path does not exist: {}'.format(image_path)
        return image_path

    # depth
    def depth_path_at(self, i):
        """
        Return the absolute path to depth i in the image sequence.
        """
        return self.depth_path_from_index(self.image_index[i])

    def depth_path_from_index(self, index):
        """
        Construct an depth path from the image's "index" identifier.
        """
        depth_path = os.path.join(self._data_path, index + '-depth' + self._image_ext)
        assert os.path.exists(depth_path), \
                'Path does not exist: {}'.format(depth_path)
        return depth_path

    # label
    def label_path_at(self, i):
        """
        Return the absolute path to metadata i in the image sequence.
        """
        return self.label_path_from_index(self.image_index[i])

    def label_path_from_index(self, index):
        """
        Construct an metadata path from the image's "index" identifier.
        """
        label_path = os.path.join(self._data_path, index + '-label' + self._image_ext)
        assert os.path.exists(label_path), \
                'Path does not exist: {}'.format(label_path)
        return label_path

    # camera pose
    def metadata_path_at(self, i):
        """
        Return the absolute path to metadata i in the image sequence.
        """
        return self.metadata_path_from_index(self.image_index[i])

    def metadata_path_from_index(self, index):
        """
        Construct an metadata path from the image's "index" identifier.
        """
        metadata_path = os.path.join(self._data_path, index + '-meta.mat')
        assert os.path.exists(metadata_path), \
                'Path does not exist: {}'.format(metadata_path)
        return metadata_path

    def gt_roidb(self):
        """
        Return the database of ground-truth regions of interest.

        This function loads/saves from/to a cache file to speed up future calls.
        """

        prefix = '_class'
        for i in range(len(self.cfg.TRAIN.CLASSES)):
            prefix += '_%d' % self.cfg.TRAIN.CLASSES[i]
        cache_file = os.path.join(self.cache_path, self.name + prefix + '_gt_roidb.pkl')
        if os.path.exists(cache_file):
            with open(cache_file, 'rb') as fid:
                roidb = cPickle.load(fid)
            print('{} gt roidb loaded from {}'.format(self.name, cache_file))
            return roidb

        gt_roidb = [self._load_ycb_video_annotation(index)
                    for index in self._image_index]

        with open(cache_file, 'wb') as fid:
            cPickle.dump(gt_roidb, fid, cPickle.HIGHEST_PROTOCOL)
        print('wrote gt roidb to {}'.format(cache_file))

        return gt_roidb


    def _load_ycb_video_annotation(self, index):
        """
        Load class name and meta data
        """
        # image path
        image_path = self.image_path_from_index(index)

        # depth path
        depth_path = self.depth_path_from_index(index)

        # label path
        label_path = self.label_path_from_index(index)

        # metadata path
        metadata_path = self.metadata_path_from_index(index)

        # parse image name
        pos = index.find('/')
        video_id = index[:pos]
        image_id = index[pos+1:]
        
        return {'image': image_path,
                'depth': depth_path,
                'label': label_path,
                'meta_data': metadata_path,
                'video_id': video_id,
                'image_id': image_id,
                'flipped': False}


    def labels_to_image(self, labels):

        height = labels.shape[0]
        width = labels.shape[1]
        im_label = np.zeros((height, width, 3), dtype=np.uint8)
        for i in range(self.num_classes):
            I = np.where(labels == i)
            im_label[I[0], I[1], :] = self._class_colors[i]

        return im_label


    def process_label_image(self, label_image):
        """
        change label image to label index
        """
        height = label_image.shape[0]
        width = label_image.shape[1]
        labels = np.zeros((height, width), dtype=np.int32)
        labels_all = np.zeros((height, width), dtype=np.int32)

        # label image is in BGR order
        index = label_image[:,:,2] + 256*label_image[:,:,1] + 256*256*label_image[:,:,0]
        for i in range(1, len(self._class_colors_all)):
            color = self._class_colors_all[i]
            ind = color[0] + 256*color[1] + 256*256*color[2]
            I = np.where(index == ind)
            labels_all[I[0], I[1]] = i

            ind = np.where(np.array(self.cfg.TRAIN.CLASSES) == i)[0]
            if len(ind) > 0:
                labels[I[0], I[1]] = ind

        return labels, labels_all


    def _load_all_poses(self):

        # load cache file
        prefix = '_class'
        for i in range(len(self.cfg.TRAIN.CLASSES)):
            prefix += '_%d' % self.cfg.TRAIN.CLASSES[i]
        cache_file = os.path.join(self.cache_path, self.name + prefix + '_poses.pkl')
        if os.path.exists(cache_file):
            with open(cache_file, 'rb') as fid:
                poses = cPickle.load(fid)
                for i in range(len(poses)):
                    print('%s, min distance %f, max distance %f' % (self._classes[i+1], np.min(poses[i][:,5]), np.max(poses[i][:,5])))
            print('{} poses loaded from {}'.format(self.name, cache_file))
            return poses

        poses = [np.zeros((0, 6), dtype=np.float32) for i in range(len(self.cfg.TRAIN.CLASSES)-1)] # no background
        classes = np.array(self.cfg.TRAIN.CLASSES)

        # load all image indexes
        image_index = self._load_image_set_index('trainval')
        print('loading poses...')
        for i in range(len(image_index)):
            filename = os.path.join(self._data_path, image_index[i] + '-meta.mat')

            meta_data = scipy.io.loadmat(filename)
            cls_indexes = meta_data['cls_indexes'].flatten()
            gt = meta_data['poses']
            if len(gt.shape) == 2:
                gt = np.reshape(gt, (3, 4, 1))

            for j in range(len(cls_indexes)):
                cls = int(cls_indexes[j])
                ind = np.where(classes == cls)[0]
                if len(ind) > 0:
                    R = gt[:, :3, j]
                    T = gt[:, 3, j]
                    pose = np.zeros((1, 6), dtype=np.float32)
                    pose[0, :3] = mat2euler(R)
                    pose[0, 3:] = T
                    poses[int(ind)-1] = np.concatenate((poses[int(ind)-1], pose), axis=0)

        # save poses
        with open(cache_file, 'wb') as fid:
            cPickle.dump(poses, fid, cPickle.HIGHEST_PROTOCOL)
        print('wrote poses to {}'.format(cache_file))
        return poses


    def evaluation(self, output_dir):

        filename = os.path.join(output_dir, 'results_posecnn.mat')
        if os.path.exists(filename):
            results_all = scipy.io.loadmat(filename)
            print('load results from file')
            print(filename)
            distances_sys = results_all['distances_sys']
            distances_non = results_all['distances_non']
            errors_rotation = results_all['errors_rotation']
            errors_translation = results_all['errors_translation']
            results_seq_id = results_all['results_seq_id'].flatten()
            results_frame_id = results_all['results_frame_id'].flatten()
            results_object_id = results_all['results_object_id'].flatten()
            results_cls_id = results_all['results_cls_id'].flatten()
        else:
            # save results
            num_max = 100000
            num_results = 2
            distances_sys = np.zeros((num_max, num_results), dtype=np.float32)
            distances_non = np.zeros((num_max, num_results), dtype=np.float32)
            errors_rotation = np.zeros((num_max, num_results), dtype=np.float32)
            errors_translation = np.zeros((num_max, num_results), dtype=np.float32)
            results_seq_id = np.zeros((num_max, ), dtype=np.float32)
            results_frame_id = np.zeros((num_max, ), dtype=np.float32)
            results_object_id = np.zeros((num_max, ), dtype=np.float32)
            results_cls_id = np.zeros((num_max, ), dtype=np.float32)

            # for each image
            count = -1
            for i in range(len(self._roidb)):
    
                # parse keyframe name
                seq_id = int(self._roidb[i]['video_id'])
                frame_id = int(self._roidb[i]['image_id'])

                # load result
                filename = os.path.join(output_dir, '%04d_%06d.mat' % (seq_id, frame_id))
                print(filename)
                result_posecnn = scipy.io.loadmat(filename)

                # load gt poses
                filename = osp.join(self._data_path, '%04d/%06d-meta.mat' % (seq_id, frame_id))
                print(filename)
                gt = scipy.io.loadmat(filename)

                # for each gt poses
                cls_indexes = gt['cls_indexes'].flatten()
                for j in range(len(cls_indexes)):
                    count += 1
                    cls_index = cls_indexes[j]
                    RT_gt = gt['poses'][:, :, j]

                    results_seq_id[count] = seq_id
                    results_frame_id[count] = frame_id
                    results_object_id[count] = j
                    results_cls_id[count] = cls_index

                    # network result
                    result = result_posecnn
                    if len(result['rois']) > 0:
                        roi_index = np.where(result['rois'][:, 1] == cls_index)[0]
                    else:
                        roi_index = []

                    if len(roi_index) > 0:
                        RT = np.zeros((3, 4), dtype=np.float32)

                        # pose from network
                        RT[:3, :3] = quat2mat(result['poses'][roi_index, :4].flatten())
                        RT[:, 3] = result['poses'][roi_index, 4:]
                        distances_sys[count, 0] = adi(RT[:3, :3], RT[:, 3],  RT_gt[:3, :3], RT_gt[:, 3], self._points[cls_index])
                        distances_non[count, 0] = add(RT[:3, :3], RT[:, 3],  RT_gt[:3, :3], RT_gt[:, 3], self._points[cls_index])
                        errors_rotation[count, 0] = re(RT[:3, :3], RT_gt[:3, :3])
                        errors_translation[count, 0] = te(RT[:, 3], RT_gt[:, 3])

                        # pose after depth refinement
                        if self.cfg.TEST.POSE_REFINE:
                            RT[:3, :3] = quat2mat(result['poses_refined'][roi_index, :4].flatten())
                            RT[:, 3] = result['poses_refined'][roi_index, 4:]
                            distances_sys[count, 1] = adi(RT[:3, :3], RT[:, 3],  RT_gt[:3, :3], RT_gt[:, 3], self._points[cls_index])
                            distances_non[count, 1] = add(RT[:3, :3], RT[:, 3],  RT_gt[:3, :3], RT_gt[:, 3], self._points[cls_index])
                            errors_rotation[count, 1] = re(RT[:3, :3], RT_gt[:3, :3])
                            errors_translation[count, 1] = te(RT[:, 3], RT_gt[:, 3])
                        else:
                            distances_sys[count, 1] = np.inf
                            distances_non[count, 1] = np.inf
                            errors_rotation[count, 1] = np.inf
                            errors_translation[count, 1] = np.inf
                    else:
                        distances_sys[count, :] = np.inf
                        distances_non[count, :] = np.inf
                        errors_rotation[count, :] = np.inf
                        errors_translation[count, :] = np.inf

            distances_sys = distances_sys[:count+1, :]
            distances_non = distances_non[:count+1, :]
            errors_rotation = errors_rotation[:count+1, :]
            errors_translation = errors_translation[:count+1, :]
            results_seq_id = results_seq_id[:count+1]
            results_frame_id = results_frame_id[:count+1]
            results_object_id = results_object_id[:count+1]
            results_cls_id = results_cls_id[:count+1]

            results_all = {'distances_sys': distances_sys,
                       'distances_non': distances_non,
                       'errors_rotation': errors_rotation,
                       'errors_translation': errors_translation,
                       'results_seq_id': results_seq_id,
                       'results_frame_id': results_frame_id,
                       'results_object_id': results_object_id,
                       'results_cls_id': results_cls_id }

            filename = os.path.join(output_dir, 'results_posecnn.mat')
            scipy.io.savemat(filename, results_all)

        # print the results
        # for each class
        import matplotlib.pyplot as plt
        max_distance = 0.1
        index_plot = [0, 1]
        color = ['r', 'b']
        leng = ['PoseCNN', 'PoseCNN refined']
        num = len(leng)
        ADD = np.zeros((self.num_classes, num), dtype=np.float32)
        ADDS = np.zeros((self.num_classes, num), dtype=np.float32)
        TS = np.zeros((self.num_classes, num), dtype=np.float32)
        classes = copy.copy(self._classes)
        classes[0] = 'all'
        for k in self.cfg.TRAIN.CLASSES:
            fig = plt.figure()
            if k == 0:
                index = range(len(results_cls_id))
            else:
                index = np.where(results_cls_id == k)[0]

            if len(index) == 0:
                continue
            print('%s: %d objects' % (classes[k], len(index)))

            # distance symmetry
            ax = fig.add_subplot(2, 3, 1)
            lengs = []
            for i in index_plot:
                D = distances_sys[index, i]
                ind = np.where(D > max_distance)[0]
                D[ind] = np.inf
                d = np.sort(D)
                n = len(d)
                accuracy = np.cumsum(np.ones((n, ), np.float32)) / n
                plt.plot(d, accuracy, color[i], linewidth=2)
                ADDS[k, i] = VOCap(d, accuracy)
                lengs.append('%s (%.2f)' % (leng[i], ADDS[k, i] * 100))
                print('%s, %s: %d objects missed' % (classes[k], leng[i], np.sum(np.isinf(D))))

            ax.legend(lengs)
            plt.xlabel('Average distance threshold in meter (symmetry)')
            plt.ylabel('accuracy')
            ax.set_title(classes[k])

            # distance non-symmetry
            ax = fig.add_subplot(2, 3, 2)
            lengs = []
            for i in index_plot:
                D = distances_non[index, i]
                ind = np.where(D > max_distance)[0]
                D[ind] = np.inf
                d = np.sort(D)
                n = len(d)
                accuracy = np.cumsum(np.ones((n, ), np.float32)) / n
                plt.plot(d, accuracy, color[i], linewidth=2)
                ADD[k, i] = VOCap(d, accuracy)
                lengs.append('%s (%.2f)' % (leng[i], ADD[k, i] * 100))
                print('%s, %s: %d objects missed' % (classes[k], leng[i], np.sum(np.isinf(D))))

            ax.legend(lengs)
            plt.xlabel('Average distance threshold in meter (non-symmetry)')
            plt.ylabel('accuracy')
            ax.set_title(classes[k])

            # translation
            ax = fig.add_subplot(2, 3, 3)
            lengs = []
            for i in index_plot:
                D = errors_translation[index, i]
                ind = np.where(D > max_distance)[0]
                D[ind] = np.inf
                d = np.sort(D)
                n = len(d)
                accuracy = np.cumsum(np.ones((n, ), np.float32)) / n
                plt.plot(d, accuracy, color[i], linewidth=2)
                TS[k, i] = VOCap(d, accuracy)
                lengs.append('%s (%.2f)' % (leng[i], TS[k, i] * 100))
                print('%s, %s: %d objects missed' % (classes[k], leng[i], np.sum(np.isinf(D))))

            ax.legend(lengs)
            plt.xlabel('Translation threshold in meter')
            plt.ylabel('accuracy')
            ax.set_title(classes[k])

            # rotation histogram
            count = 4
            for i in index_plot:
                ax = fig.add_subplot(2, 3, count)
                D = errors_rotation[index, i]
                ind = np.where(np.isfinite(D))[0]
                D = D[ind]
                ax.hist(D, bins=range(0, 190, 10), range=(0, 180))
                plt.xlabel('Rotation angle error')
                plt.ylabel('count')
                ax.set_title(leng[i])
                count += 1

            # mng = plt.get_current_fig_manager()
            # mng.full_screen_toggle()
            filename = output_dir + '/' + classes[k] + '.png'
            plt.savefig(filename)
            # plt.show()

        # print ADD
        print('==================ADD======================')
        for k in self.cfg.TRAIN.CLASSES:
            print('%s: %f' % (classes[k], ADD[k, 0]))
        for k in self.cfg.TRAIN.CLASSES[1:]:
            print('%f' % (ADD[k, 0]))
        print('%f' % (ADD[0, 0]))
        print(self.cfg.TRAIN.SNAPSHOT_INFIX)
        print('===========================================')

        # print ADD-S
        print('==================ADD-S====================')
        for k in self.cfg.TRAIN.CLASSES:
            print('%s: %f' % (classes[k], ADDS[k, 0]))
        for k in self.cfg.TRAIN.CLASSES[1:]:
            print('%f' % (ADDS[k, 0]))
        print('%f' % (ADDS[0, 0]))
        print(self.cfg.TRAIN.SNAPSHOT_INFIX)
        print('===========================================')
