# ------------------------------------------------------------------------
# PoET: Pose Estimation Transformer for Single-View, Multi-Object 6D Pose Estimation
# Copyright (c) 2022 Thomas Jantos (thomas.jantos@aau.at), University of Klagenfurt - Control of Networked Systems (CNS). All Rights Reserved.
# Licensed under the BSD-2-Clause-License with no commercial use [see LICENSE for details]
# ------------------------------------------------------------------------
# Modified from Deformable DETR (https://github.com/fundamentalvision/Deformable-DETR)
# Copyright (c) 2020 SenseTime. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE_DEFORMABLE_DETR in the LICENSES folder for details]
# ------------------------------------------------------------------------
# Modified from DETR (https://github.com/facebookresearch/detr)
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# ------------------------------------------------------------------------

import json

import cv2
import torch
import util.misc as utils

from data_utils.data_prefetcher import data_prefetcher
from models import build_model
from inference_tools.dataset import build_dataset
from torch.utils.data import DataLoader, SequentialSampler
import time
import os
import supervision as sv
import numpy as np


def transform_bbox(bbox, img_size):
    """
    Convert normalized cxcywh bounding box coordinates to pixel coordinates in xyxy format.

    Args:
    - normalized_bbox: Tuple of (cx, cy, w, h) normalized to [0, 1]
    - image_size: Tuple of (image_width, image_height) in pixels

    Returns:
    - Tuple of (x1_pixel, y1_pixel, x2_pixel, y2_pixel)
    """
    cx_normalized, cy_normalized, w_normalized, h_normalized = bbox
    image_width, image_height = img_size

    # Convert normalized values to pixel values
    x_center_pixel = cx_normalized * image_width
    y_center_pixel = cy_normalized * image_height
    width_pixel = w_normalized * image_width
    height_pixel = h_normalized * image_height

    # Calculate top-left and bottom-right corners
    x1_pixel = x_center_pixel - (width_pixel / 2)
    y1_pixel = y_center_pixel - (height_pixel / 2)
    x2_pixel = x_center_pixel + (width_pixel / 2)
    y2_pixel = y_center_pixel + (height_pixel / 2)

    return [x1_pixel, y1_pixel, x2_pixel, y2_pixel]


def inference(args):
    """
    Script for Inference with PoET. The datalaoder loads all the images and then iterates over them. PoET processes each
    image and stores the detected objects and their poses in a JSON file. Currently, this script allows only batch sizes
    of 1.
    """
    device = torch.device(args.device)
    model, criterion, matcher = build_model(args)
    model.to(device)
    model.eval()

    # Load model weights
    checkpoint = torch.load(args.resume, map_location='cpu')
    model.load_state_dict(checkpoint['model'], strict=False)

    # Construct dataloader that loads the images for inference
    dataset_inference = build_dataset(args)
    sampler_inference = SequentialSampler(dataset_inference)
    data_loader_inference = DataLoader(dataset_inference, 1, sampler=sampler_inference,
                                       drop_last=False, collate_fn=utils.collate_fn, num_workers=0,
                                       pin_memory=True)

    prefetcher = data_prefetcher(data_loader_inference, device, prefetch=False)
    samples, targets = prefetcher.next()
    results = {}

    if not os.path.exists(args.inference_output + "bbox/"):
        os.makedirs(args.inference_output + "bbox/")

    start = time.time()

    # Iterate over all images, perform pose estimation and store results.
    for i, idx in enumerate(range(len(data_loader_inference))):
        print("Processing {}/{}".format(i, len(data_loader_inference) - 1))

        start_ = time.time()
        outputs, n_boxes_per_sample = model(samples, targets)
        print(f"took: {time.time() - start_:.4f}s")

        if outputs is None:
            continue

        # Iterate over all the detected predictions
        img_file = data_loader_inference.dataset.image_paths[i]
        img_id = img_file[img_file.find("_")+1:img_file.rfind(".")]
        results[img_id] = {}
        for d in range(n_boxes_per_sample[0]):
            pred_t = outputs['pred_translation'][0][d].detach().cpu().tolist()
            pred_rot = outputs['pred_rotation'][0][d].detach().cpu().tolist()
            pred_box = outputs['pred_boxes'][0][d].detach().cpu().tolist()
            pred_class = outputs['pred_classes'][0][d].detach().cpu().tolist()
            results[img_id][d] = {
                "t": pred_t,
                "rot": pred_rot,
                "box": pred_box, # format: cxcywh
                "class": pred_class
            }

            # Draw predicted bounding box and save to output folder
            detections = sv.Detections(xyxy=np.array([transform_bbox(pred_box, (640, 480))])) # transform normalized cxcywh to xyxy
            box_annotator = sv.BoxAnnotator()
            annotated_frame = cv2.imread(args.inference_path + img_file)
            annotated_frame = box_annotator.annotate(scene=annotated_frame, detections=detections)
            cv2.imwrite(args.inference_output + "bbox/" + img_id + ".png", annotated_frame)
            # cv2.imshow('frame', annotated_frame)
            # cv2.waitKey(-1)
            # cv2.destroyAllWindows()

        samples, targets = prefetcher.next()

    print("-------------------")
    print(f"Total took: {time.time() - start:.4f}s")
    print(f"Avrg. took: {(time.time() - start) / len(data_loader_inference):.4f}s")
    print(f"FPS: {1.0 / ((time.time() - start) / len(data_loader_inference)):.4f}")
    # Store the json-file
    out_file_name = "results.json"
    with open(args.inference_output + out_file_name, 'w') as out_file:
        json.dump(results, out_file)
    return

