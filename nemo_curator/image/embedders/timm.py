# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
from typing import Iterable, Optional

import nvidia.dali.fn as fn
import nvidia.dali.types as types
import timm
import torch
from nvidia.dali import pipeline_def
from nvidia.dali.plugin.pytorch import feed_ndarray

from nemo_curator.image.embedders.base import ImageEmbedder
from nemo_curator.utils.image.transforms import convert_transforms_to_dali


class TimmImageEmbedder(ImageEmbedder):
    def __init__(
        self,
        model_name: str,
        pretrained: bool = False,
        batch_size: int = 1,
        num_threads_per_worker: int = 4,
        image_embedding_column: str = "image_embedding",
        normalize_embeddings: bool = True,
        classifiers: Iterable = [],
    ) -> None:
        super().__init__(
            model_name=model_name,
            image_embedding_column=image_embedding_column,
            classifiers=classifiers,
        )
        self.pretrained = pretrained
        self.batch_size = batch_size
        self.num_threads_per_worker = num_threads_per_worker
        self.normalize_embeddings = normalize_embeddings

        # Load the model to get the transforms
        model = timm.create_model(self.model_name, pretrained=self.pretrained)
        torch_transforms = timm.data.create_transform(
            **timm.data.resolve_data_config(model.pretrained_cfg)
        )
        self.dali_transforms = convert_transforms_to_dali(torch_transforms)

    def load_dataset_shard(self, tar_path: str, device_id=0):
        # Create the DALI pipeline
        @pipeline_def(
            batch_size=self.batch_size,
            num_threads=self.num_threads_per_worker,
            device_id=device_id,
        )
        def webdataset_pipeline(_tar_path: str):
            img_raw, text, json = fn.readers.webdataset(
                paths=_tar_path,
                ext=["jpg", "txt", "json"],
                missing_component_behavior="error",
            )
            img = fn.decoders.image(img_raw, device="mixed", output_type=types.RGB)

            for transform in self.dali_transforms:
                img = transform(img)

            return img, text, json

        pipeline = webdataset_pipeline(tar_path)
        pipeline.build()

        total_samples = pipeline.epoch_size()
        total_samples = total_samples[list(total_samples.keys())[0]]

        samples_completed = 0
        while samples_completed < total_samples:
            image, text, meta = pipeline.run()
            image = image.as_tensor()

            image_torch = torch.empty(
                image.shape(), dtype=torch.float32, device=f"cuda:{device_id}"
            )
            feed_ndarray(image, image_torch)  # COPY !!!
            image = image_torch

            captions = [text.at(i).tostring().decode("utf-8") for i in range(len(text))]
            metadata = [
                json.loads(meta.at(i).tostring().decode("utf-8"))
                for i in range(len(meta))
            ]

            remaining_samples = total_samples - samples_completed
            if image.shape[0] >= remaining_samples:
                image = image[:remaining_samples]
                captions = captions[:remaining_samples]
                metadata = metadata[:remaining_samples]

            samples_completed += min(image.shape[0], remaining_samples)

            yield image, metadata

    def load_embedding_model(self, device="cuda"):
        model = timm.create_model(self.model_name, pretrained=self.pretrained).eval()
        model = model.to(device)

        def infer(batch):
            image_features = model(batch)
            if self.normalize_embeddings:
                image_features = self.torch_normalized(image_features)

            return image_features

        return infer

    @staticmethod
    def torch_normalized(a, dim=-1):
        return torch.nn.functional.normalize(a, dim=dim)