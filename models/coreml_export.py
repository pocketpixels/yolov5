"""Exports a YOLOv5 *.pt model to ONNX and TorchScript formats

Usage:
    $ export PYTHONPATH="$PWD" && python models/export.py --weights yolov5s.pt --img 640 --batch 1
"""

import argparse
import sys
import os
import time
from pathlib import Path

sys.path.append(Path(__file__).parent.parent.absolute().__str__())  # to run '$ python *.py' files in subdirectories

import torch
import torch.nn as nn
from torch.utils.mobile_optimizer import optimize_for_mobile

import models
from models.experimental import attempt_load
from utils.activations import Hardswish, SiLU
from utils.general import colorstr, check_img_size, check_requirements, file_size, set_logging
from utils.torch_utils import select_device


class ExportModel(nn.Module):
    def __init__(self, base_model, img_size):
        super(ExportModel, self).__init__()
        self.base_model = base_model
        self.img_size = img_size

    def forward(self, x):
        x = self.base_model(x)[0]
        x = x.squeeze(0)
        # Convert box coords to normalized coordinates [0 ... 1]
        w = self.img_size[0]
        h = self.img_size[1]
        objectness = x[:, 4:5]
        class_probs = x[:, 5:] * objectness
        boxes = x[:, :4] * torch.tensor([1./w, 1./h, 1./w, 1./h])
        # predictions = x[:, 4:]
        return (class_probs, boxes)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--weights', type=str, default='./yolov5s.pt', help='weights path')
    parser.add_argument('--img-size', nargs='+', type=int, default=[640, 640], help='image size')  # height, width
    parser.add_argument('--device', default='cpu', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--labels', nargs='+', type=str, help='class labels, as text file or directly, space separated')

    opt = parser.parse_args()
    opt.img_size *= 2 if len(opt.img_size) == 1 else 1  # expand
    print(opt)
    set_logging()
    t = time.time()

    # get labels
    labels = []
    if opt.labels:
        if len(opt.labels) == 1 and os.path.isfile(opt.labels[0]):
            with open(opt.labels[0], "r") as f:
                labels = f.read().replace(",", " ").split()
        else:
            labels = opt.labels

    # Load PyTorch model
    device = select_device(opt.device)
    model = attempt_load(opt.weights, map_location=device)  # load FP32 model
    export_model = ExportModel(model, img_size=opt.img_size)

    # Checks
    gs = int(max(model.stride))  # grid size (max stride)
    opt.img_size = [check_img_size(x, gs) for x in opt.img_size]  # verify img_size are gs-multiples

    # Input
    img = torch.zeros(1, 3, *opt.img_size).to(device)  # image size(1,3,320,192) iDetection

    # Update model
    for k, m in model.named_modules():
        m._non_persistent_buffers_set = set()  # pytorch 1.6.0 compatibility
        if isinstance(m, models.common.Conv):  # assign export-friendly activations
            if isinstance(m.act, nn.Hardswish):
                m.act = Hardswish()
            elif isinstance(m.act, nn.SiLU):
                m.act = SiLU()
        elif isinstance(m, models.yolo.Detect):
            m.inplace = False

    for _ in range(2):
        y = model(img)  # dry runs

    num_boxes = y[0].shape[1]
    num_classes = y[0].shape[2] - 5

    if labels:
        assert len(labels) == num_classes, f'The number of labels specified ({len(labels)}) does not match the number of classes in the model ({num_classes})'
    else:
        print("No class labels specified, using generic labels \"Class 1\", \"Class 2\" ...")
        labels = [f"Class {n+1}" for n in range(num_classes)]

    print(f"\n{colorstr('PyTorch:')} starting from {opt.weights} ({file_size(opt.weights):.1f} MB)")

    # TorchScript export -----------------------------------------------------------------------------------------------
    prefix = colorstr('TorchScript:')
    try:
        print(f'\n{prefix} starting export with torch {torch.__version__}...')
        f = opt.weights.replace('.pt', '.torchscript.pt')  # filename
        ts = torch.jit.trace(export_model, img, strict=False)
        # ts = optimize_for_mobile(ts)
    except Exception as e:
        print(f'{prefix} export failure: {e}')

    # CoreML export ----------------------------------------------------------------------------------------------------
    prefix = colorstr('CoreML:')
    try:
        import coremltools as ct
        from coremltools.models.pipeline import Pipeline
        from coremltools.models import datatypes
        import coremltools.proto.FeatureTypes_pb2 as ft

        print(f'{prefix} starting export with coremltools {ct.__version__}...')
        # convert model from torchscript and apply pixel scaling as per detect.py
        orig_model = ct.convert(ts, inputs=[ct.ImageType(name='image', shape=img.shape, scale=1 / 255.0, bias=[0, 0, 0])])

        spec = orig_model.get_spec()
        old_box_output_name = spec.description.output[1].name
        old_scores_output_name = spec.description.output[0].name
        ct.utils.rename_feature(spec, old_scores_output_name, "raw_confidence")
        ct.utils.rename_feature(spec, old_box_output_name, "raw_coordinates")
        spec.description.output[0].type.multiArrayType.shape.extend([num_boxes, num_classes])
        spec.description.output[1].type.multiArrayType.shape.extend([num_boxes, 4])
        spec.description.output[0].type.multiArrayType.dataType = ft.ArrayFeatureType.DOUBLE
        spec.description.output[1].type.multiArrayType.dataType = ft.ArrayFeatureType.DOUBLE

        yolo_model = ct.models.MLModel(spec)

        # Build Non Maximum Suppression model
        nms_spec = ct.proto.Model_pb2.Model()
        nms_spec.specificationVersion = 3

        for i in range(2):
            decoder_output = spec.description.output[i].SerializeToString()

            nms_spec.description.input.add()
            nms_spec.description.input[i].ParseFromString(decoder_output)

            nms_spec.description.output.add()
            nms_spec.description.output[i].ParseFromString(decoder_output)

        nms_spec.description.output[0].name = "confidence"
        nms_spec.description.output[1].name = "coordinates"

        output_sizes = [num_classes, 4]
        for i in range(2):
            ma_type = nms_spec.description.output[i].type.multiArrayType
            ma_type.shapeRange.sizeRanges.add()
            ma_type.shapeRange.sizeRanges[0].lowerBound = 0
            ma_type.shapeRange.sizeRanges[0].upperBound = -1
            ma_type.shapeRange.sizeRanges.add()
            ma_type.shapeRange.sizeRanges[1].lowerBound = output_sizes[i]
            ma_type.shapeRange.sizeRanges[1].upperBound = output_sizes[i]
            del ma_type.shape[:]

        nms = nms_spec.nonMaximumSuppression
        nms.confidenceInputFeatureName = "raw_confidence"
        nms.coordinatesInputFeatureName = "raw_coordinates"
        nms.confidenceOutputFeatureName = "confidence"
        nms.coordinatesOutputFeatureName = "coordinates"
        nms.iouThresholdInputFeatureName = "iouThreshold"
        nms.confidenceThresholdInputFeatureName = "confidenceThreshold"

        default_iou_threshold = 0.45
        default_confidence_threshold = 0.6
        nms.iouThreshold = default_iou_threshold
        nms.confidenceThreshold = default_confidence_threshold
        nms.pickTop.perClass = True
        nms.stringClassLabels.vector.extend(labels)

        nms_model = ct.models.MLModel(nms_spec)

        # Assembling a pipeline model from the two models

        input_features = [("image", datatypes.Array(3, 300, 300)),
                          ("iouThreshold", datatypes.Double()),
                          ("confidenceThreshold", datatypes.Double())]

        output_features = ["confidence", "coordinates"]

        pipeline = Pipeline(input_features, output_features)

        pipeline.add_model(yolo_model)
        pipeline.add_model(nms_model)

        # The "image" input should really be an image, not a multi-array.
        pipeline.spec.description.input[0].ParseFromString(spec.description.input[0].SerializeToString())

        # Copy the declarations of the "confidence" and "coordinates" outputs.
        # The Pipeline makes these strings by default.
        pipeline.spec.description.output[0].ParseFromString(nms_spec.description.output[0].SerializeToString())
        pipeline.spec.description.output[1].ParseFromString(nms_spec.description.output[1].SerializeToString())

        # Add descriptions to the inputs and outputs.
        pipeline.spec.description.input[1].shortDescription = "(optional) IOU Threshold override"
        pipeline.spec.description.input[2].shortDescription = "(optional) Confidence Threshold override"
        pipeline.spec.description.output[0].shortDescription = u"Boxes Class confidence"
        pipeline.spec.description.output[1].shortDescription = u"Boxes [x, y, width, height] (relative to image size)"

        # Add metadata to the model.
        pipeline.spec.description.metadata.shortDescription = "YOLO v5 card detector"
        pipeline.spec.description.metadata.author = "Pocket Pixels Inc."

        # Add the list of class labels and the default threshold values too.
        user_defined_metadata = {
            "iou_threshold": str(default_iou_threshold),
            "confidence_threshold": str(default_confidence_threshold),
            "classes": ", ".join(labels)
        }
        pipeline.spec.description.metadata.userDefined.update(user_defined_metadata)

        # Don't forget this or Core ML might attempt to run the model on an unsupported
        # operating system version!
        pipeline.spec.specificationVersion = 3

        final_model = ct.models.MLModel(pipeline.spec)

        f = opt.weights.replace('.pt', '.mlmodel')  # filename
        final_model.save(f)
        print(f'{prefix} export success, saved as {f} ({file_size(f):.1f} MB)')
    except Exception as e:
        print(f'{prefix} export failure: {e}')

    # Finish
    print(f'\nExport complete ({time.time() - t:.2f}s). Visualize with https://github.com/lutzroeder/netron.')
