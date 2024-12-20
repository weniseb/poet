# Copyright (c) 2022 University of Klagenfurt - Control of Networked Systems (CNS). All Rights Reserved.
# Author: Thomas Jantos (thomas.jantos@aau.at)
import bisect
import json
import os
from collections import OrderedDict

import numpy as np
import torch
import torch.nn.functional as F
from sklearn.feature_extraction.text import TfidfVectorizer
from torch import Tensor, nn
from typing import Dict, Optional, List, Tuple

from constants import ROOT_DIR
from .yolo.backbone_models.models import Darknet, load_darknet_weights
from .yolo.yolo_utils.general import non_max_suppression
from .yolo.yolo_utils.torch_utils import select_device

from torchvision.ops import box_convert
from .groundingdino.util.utils import get_phrases_from_posmap
from sklearn.metrics.pairwise import cosine_similarity
from .groundingdino.util.slconfig import SLConfig
from .groundingdino.models.GroundingDINO import build_groundingdino
from .groundingdino.util.misc import clean_state_dict
import models.groundingdino.datasets.transforms as T

from util.misc import NestedTensor
from util import logger

import supervision as sv
import cv2


class YOLOBackbone(Darknet):
    """
    YOLOv4 Object Detector.
    Returns detected/predicted objects (class, bounding box) and the feature maps.
    """
    def __init__(self, args, train_backbone: bool, return_interm_layers: bool, return_layers=[142, 157, 172]):

        # TODO: Check whether Imagesize argument is needed
        super().__init__(args.backbone_cfg)

        self.return_iterm_layers = return_interm_layers

        if return_interm_layers:
            self.return_layers = {str(i): v for i, v in enumerate(return_layers)}
            self.num_channels = [x[0].out_channels for x in [self.module_list[i] for i in return_layers]]
            self.strides = [x[0].stride[0] for x in [self.module_list[i] for i in return_layers]]
        else:
            self.return_layers = {'0': 172}
            self.num_channels = self.module_list[return_layers[0]].out_channels
            self.strides = self.module_list[return_layers[0]].strides

        # Set threshold parameters
        self.conf_thres = args.backbone_conf_thresh
        self.iou_thres = args.backbone_iou_thresh
        self.agnostic_nms = args.backbone_agnostic_nms

        # Freeze backbone if it should not be trained
        self.train_backbone = train_backbone
        if not train_backbone:
            for name, parameter in self.named_parameters():
                parameter.requires_grad_(False)

    def forward_backbone(self, x, verbose=False):
        yolo_out, out = [], []
        intermediate = OrderedDict()
        intermediate_i = 0
        if verbose:
            print('0', x.shape)
            str_o = ''

        # Passing the image through the YOLO model layer by layer
        for i, module in enumerate(self.module_list):
            name = module.__class__.__name__
            if name in ['WeightedFeatureFusion', 'FeatureConcat', 'FeatureConcat2', 'FeatureConcat3',
                        'FeatureConcat_l']:  # sum, concat
                if verbose:
                    l = [i - 1] + module.layers  # layers
                    sh = [list(x.shape)] + [list(out[i].shape) for i in module.layers]  # shapes
                    str_o = ' >> ' + ' + '.join(['layer %g %s' % x for x in zip(l, sh)])
                x = module(x, out)  # WeightedFeatureFusion(), FeatureConcat()
            elif name == 'YOLOLayer':
                yolo_out.append(module(x, out))
            else:  # run module directly, i.e. mtype = 'convolutional', 'upsample', 'maxpool', 'batchnorm2d' etc.
                x = module(x)

            if i in self.return_layers.values():
                intermediate[str(intermediate_i)] = x
                intermediate_i += 1

            out.append(x if self.routs[i] else [])
            if verbose:
                print('%g/%g %s -' % (i, len(self.module_list), name), list(x.shape), str_o)
                str_o = ''

        if self.training:
            # TODO: Write code when backbone is not frozen
            # We want to return the same as the original yolo, but also the predicted outputs as we need them for further processing
            raise NotImplementedError
        else:
            x, p = zip(*yolo_out)  # inference output, training output
            x = torch.cat(x, 1)  # cat yolo outputs
            # Determine prediction from yolo output layers: pred = [bbox (4), conf, class]
            pred = non_max_suppression(x, self.conf_thres, self.iou_thres, classes=None,
                                       agnostic=self.agnostic_nms)
        return pred, intermediate

    def forward_once(self, tensor_list, augment=False, verbose=False):
        # Pass Image through YOLO
        predictions, xs = self.forward_backbone(tensor_list.tensors)
        # Adjust predicted classes by 1 as class 0 is "background / dummy" in PoET
        for prediction in predictions:
            if prediction is not None:
                prediction[:, 5] += 1

        out: Dict[str, NestedTensor] = {}
        for name, x in xs.items():
            m = tensor_list.mask
            assert m is not None
            mask = F.interpolate(m[None].float(), size=x.shape[-2:]).to(torch.bool)[0]
            out[name] = NestedTensor(x, mask)
        return predictions, out


class YOLODINOBackbone(nn.Module):
  def __init__(self, yolo_backbone=None, dino_backbone=None, args=None) -> None:
    super(YOLODINOBackbone, self).__init__()

    self.args = args
    self.device = args.device if args.device else "cuda"

    assert yolo_backbone is not None
    self.yolo_backbone = yolo_backbone

    assert dino_backbone is not None
    self.dino_backbone = dino_backbone

    self.class_mode = args.class_mode if args.class_mode else "specific"

    self.strides = self.yolo_backbone.strides
    self.num_channels = self.yolo_backbone.num_channels
    self.return_layers = self.yolo_backbone.return_layers
    self.train_backbone = self.yolo_backbone.train_backbone
    if not self.train_backbone:
      for name, parameter in self.named_parameters():
        parameter.requires_grad_(False)

    if args.class_info[0] == "/":
        args.class_info = args.class_info[1:]

    class_info = os.path.join(args.dataset_path, args.class_info)
    assert class_info is not None

    with open(class_info, 'r') as f:
      self.class_info = json.load(f)

    dataset = args.dataset if args.dataset else "ycbv"

    self.map = {}
    if dataset == "ycbv":
      for key, value in self.class_info.items():
        new_key = value[4:].replace('_', ' ')
        self.map[new_key] = int(key)
    elif dataset == "icmi":
      for key, value in self.class_info.items():
        new_key = value.replace('_', ' ')
        self.map[new_key] = int(key)
    else:
      for key, value in self.class_info.items():
        self.map[value] = int(key)

    self.vectorizer = TfidfVectorizer()
    self.tfidf = self.vectorizer.fit_transform(self.map.keys())

    ## TODO: Refactore single label classification caption for dino
    if self.class_mode == "agnostic":
        if not args.dino_caption:
            logger.warn(f"Class mode is '{self.class_mode}', using default dino caption 'object in the middle.'!")
            self.dino_caption = "object in the middle."
        else:
            self.dino_caption = args.dino_caption
    else:
        if args.dino_caption:
            logger.warn(f"Class mode is '{self.class_mode}', ignoring provided dino caption '{args.dino_caption}'!")
        self.dino_caption = ". ".join(list(self.map.keys()))
        self.dino_caption = self.dino_caption.replace("_", " ")

  def dinoPredict(self, images: torch.Tensor, caption_: str, box_threshold: float, text_threshold: float,
                  remove_combined: bool = False) -> Tuple[torch.Tensor, torch.Tensor, List[str]]:

    # TODO: Integrate "phrase" mode of groundingdino! (inference_on_an_image.py)
    # "preprocess_caption()"
    caption = caption_.lower().strip()
    if not caption.endswith("."):
      caption = caption + "."

    model = self.dino_backbone.to(self.device)
    images = images.to(self.device)

    with torch.no_grad():
      outputs = model(images, captions=[caption for _ in range(len(images))])

    for idx, _ in enumerate(range(outputs["pred_boxes"].shape[0])):
      prediction_logits = outputs["pred_logits"][idx].sigmoid()  # prediction_logits.shape = (nq, 256)
      prediction_boxes = outputs["pred_boxes"][idx]  # prediction_boxes.shape = (nq, 4)

      mask = prediction_logits.max(dim=1)[0] > box_threshold
      logits = prediction_logits[mask]  # logits.shape = (n, 256)
      boxes = prediction_boxes[mask]  # boxes.shape = (n, 4)

      tokenizer = model.tokenizer
      tokenized = tokenizer(caption)

      if remove_combined:
        sep_idx = [i for i in range(len(tokenized['input_ids'])) if tokenized['input_ids'][i] in [101, 102, 1012]]

        phrases = []
        for logit in logits:
          max_idx = logit.argmax()
          insert_idx = bisect.bisect_left(sep_idx, max_idx)
          right_idx = sep_idx[insert_idx]
          left_idx = sep_idx[insert_idx - 1]
          phrases.append(
            get_phrases_from_posmap(logit > text_threshold, tokenized, tokenizer, left_idx, right_idx).replace('.',
                                                                                                               ''))
      else:
        phrases = [
          get_phrases_from_posmap(logit > text_threshold, tokenized, tokenizer).replace('.', '')
          for logit
          in logits
        ]

      yield boxes, logits.max(dim=1)[0], phrases

  def normalizeImages(self, images):
    """
    Normalizes tensor of images for GroundingDINO.
    """
    transform = T.Compose(
      [
        # T.RandomResize([800], max_size=1333),
        # T.ToTensor(),
        T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
      ]
    )
    transformed, _ = transform(images, None)
    return transformed

  def forward(self, tensor_list: NestedTensor):
    # Pass Image through YOLO for feature maps
    _, out = self.yolo_backbone(tensor_list)

    # [bbox, score, label]
    images = self.normalizeImages(tensor_list.tensors)
    raw_images = tensor_list.tensors

    predictions = []
    for idx, (boxes, logits, phrases) in enumerate(
        self.dinoPredict(images=images, caption_=self.dino_caption, box_threshold=self.args.dino_box_threshold, text_threshold=self.args.dino_txt_threshold)):
      image = raw_images[idx]

      # Unnormalize bboxes (Predicted bboxes are in normalized "cxcywh" format!!)
      _, h, w = image.shape  # h, w, c
      boxes = boxes * torch.Tensor([w, h, w, h]).to(self.device)
      # PoET expects xyxy format, later in "pose_estimation_transformer.py" it will be converted back to (normalized) cxcywh
      boxes = box_convert(boxes, "cxcywh","xyxy")

      ################################
      # BBox Visualization
      #
      if self.args.dino_bbox_viz:
          labels = [
            f"{phrase} {logit:.2f}"
            for phrase, logit
            in zip(phrases, logits)
          ]
          detections = sv.Detections(xyxy=boxes.numpy())
          box_annotator = sv.BoxAnnotator()
          annotated_frame = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
          annotated_frame = box_annotator.annotate(scene=annotated_frame, detections=detections, labels=labels)
          cv2.imshow('frame', annotated_frame)
          cv2.waitKey(-1)
          cv2.destroyAllWindows()

      if not len(boxes) == 0:
        p = []
        for box, logits, label in zip(boxes, logits, phrases):
          pred = None
          if self.class_mode == "specific":
            label_vec = self.vectorizer.transform([label])
            cos_sim = cosine_similarity(label_vec, self.tfidf).flatten()

            if all(v <= self.args.dino_cos_sim for v in cos_sim):  # If not a single label matches 10% of pred label
              continue

            best_match_idx = np.argmax(cos_sim)
            best_match = list(self.map.keys())[best_match_idx]
            cls = self.map[best_match]

            pred = torch.hstack((box, logits, torch.tensor(cls).to("cuda")))
          else:
            pred = torch.hstack((box, logits, torch.tensor(-1).to("cuda")))
          p.append(pred)

        # If all predictions are below label matching threshold
        if len(p) != 0:
          p = torch.stack(p)
          predictions.append(p)
        else:
          predictions.append(None)
      else:
        predictions.append(None)

    return predictions, out

def build_yolo_dino(args):
    args_dino = SLConfig.fromfile(os.path.join(ROOT_DIR, args.dino_args))
    args_dino.device = "cuda"
    dino = build_groundingdino(args_dino)

    checkpoint = torch.load(os.path.join(ROOT_DIR, args.dino_checkpoint), map_location="cpu")
    dino.load_state_dict(clean_state_dict(checkpoint["model"]), strict=False)
    dino.eval()

    train_backbone = args.lr_backbone > 0
    return_interm_layers = (args.num_feature_levels > 1)
    cns_yolo = YOLOBackbone(args, train_backbone, return_interm_layers)
    if args.backbone_weights is not None:
        try:
            cns_yolo.load_state_dict(torch.load(args.backbone_weights, map_location=select_device())['model'])
        except Exception as e:
            print(e)
            load_darknet_weights(cns_yolo, args.backbone_weights)

    yolo_dino = YOLODINOBackbone(yolo_backbone=cns_yolo, dino_backbone=dino, args=args)
    return yolo_dino
