import base64
import io
from datetime import datetime
from typing import Dict, Optional, Tuple

import numpy as np
import requests
import torch
from PIL import Image


MODEL_OPTIONS = [
    "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image-preview",
]

ASPECT_RATIO_OPTIONS = [
    "1:1",
    "16:9",
    "9:16",
    "4:3",
    "3:4",
    "3:2",
    "2:3",
    "21:9",
    "5:4",
    "4:5",
]

RESOLUTION_OPTIONS = ["2K", "4K"]
TIMEOUT_MAP: Dict[str, int] = {"1K": 180, "2K": 300, "4K": 360}


def _build_api_url(model_name: str) -> str:
    return f"https://api.apiyi.com/v1beta/models/{model_name}:generateContent"


def _tensor_to_base64_png(image_tensor: torch.Tensor) -> str:
    """
    ComfyUI IMAGE tensor: [B, H, W, C], range 0~1.
    This helper converts the first image in batch to base64-encoded PNG.
    """
    if image_tensor.dim() != 4 or image_tensor.shape[0] < 1:
        raise ValueError("输入图片格式不正确，期望 IMAGE(batch, height, width, channel)。")

    image_np = image_tensor[0].cpu().numpy()
    image_np = np.clip(image_np * 255.0, 0, 255).astype(np.uint8)
    pil_img = Image.fromarray(image_np)

    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _base64_to_tensor(base64_str: str) -> torch.Tensor:
    image_bytes = base64.b64decode(base64_str)
    pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    image_np = np.array(pil_img).astype(np.float32) / 255.0
    return torch.from_numpy(image_np)[None, ...]


def _post_generation_request(
    api_key: str,
    model_name: str,
    prompt: str,
    aspect_ratio: str,
    resolution: str,
    image_payload_parts: Optional[list] = None,
) -> Tuple[torch.Tensor, str]:
    if not api_key or api_key.strip() == "":
        raise ValueError("API Key 不能为空，请在节点里填写你的 API Key。")
    if not prompt or prompt.strip() == "":
        raise ValueError("提示词不能为空。")

    parts = []
    if image_payload_parts:
        parts.extend(image_payload_parts)
    parts.append({"text": prompt})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {
                "aspectRatio": aspect_ratio,
                "imageSize": resolution,
            },
        },
    }

    url = _build_api_url(model_name)
    timeout = TIMEOUT_MAP.get(resolution, 300)
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }

    response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(
            f"API 请求失败，状态码 {response.status_code}，返回内容: {response.text}"
        )

    result = response.json()
    candidates = result.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"API 未返回候选结果: {result}")

    content = candidates[0].get("content", {})
    resp_parts = content.get("parts", [])
    if not resp_parts:
        raise RuntimeError(f"API 返回内容中未找到图片数据: {result}")

    inline_data = resp_parts[0].get("inlineData", {})
    image_data = inline_data.get("data")
    if not image_data:
        raise RuntimeError(f"API 返回数据缺少 inlineData.data 字段: {result}")

    image_tensor = _base64_to_tensor(image_data)
    output_name = f"apiyi_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    return image_tensor, output_name


class APIYITextToImageNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_key": ("STRING", {"default": "sk-your-api-key", "multiline": False}),
                "model_name": (MODEL_OPTIONS,),
                "prompt": ("STRING", {"default": "一只可爱的橘猫，电影感打光，超清细节", "multiline": True}),
                "aspect_ratio": (ASPECT_RATIO_OPTIONS,),
                "resolution": (RESOLUTION_OPTIONS,),
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "filename")
    FUNCTION = "generate"
    CATEGORY = "APIYI/Image"

    def generate(
        self,
        api_key: str,
        model_name: str,
        prompt: str,
        aspect_ratio: str,
        resolution: str,
    ):
        image_tensor, output_name = _post_generation_request(
            api_key=api_key,
            model_name=model_name,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            image_payload_parts=None,
        )
        return (image_tensor, output_name)


class APIYIMultiImageEditNode:
    """
    最多支持 5 张图片：
    - image_1 必填
    - image_2 ~ image_5 选填
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_key": ("STRING", {"default": "sk-your-api-key", "multiline": False}),
                "model_name": (MODEL_OPTIONS,),
                "prompt": (
                    "STRING",
                    {
                        "default": "将多张图片融合为一张专业、自然、细节清晰的画面。",
                        "multiline": True,
                    },
                ),
                "aspect_ratio": (ASPECT_RATIO_OPTIONS,),
                "resolution": (RESOLUTION_OPTIONS,),
                "image_1": ("IMAGE",),
            },
            "optional": {
                "image_2": ("IMAGE",),
                "image_3": ("IMAGE",),
                "image_4": ("IMAGE",),
                "image_5": ("IMAGE",),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING", "INT")
    RETURN_NAMES = ("image", "filename", "used_image_count")
    FUNCTION = "edit"
    CATEGORY = "APIYI/Image"

    def edit(
        self,
        api_key: str,
        model_name: str,
        prompt: str,
        aspect_ratio: str,
        resolution: str,
        image_1: torch.Tensor,
        image_2: Optional[torch.Tensor] = None,
        image_3: Optional[torch.Tensor] = None,
        image_4: Optional[torch.Tensor] = None,
        image_5: Optional[torch.Tensor] = None,
    ):
        images = [image_1, image_2, image_3, image_4, image_5]
        images = [img for img in images if img is not None]
        if len(images) == 0:
            raise ValueError("至少需要提供 1 张输入图片。")
        if len(images) > 5:
            raise ValueError("最多仅支持 5 张输入图片。")

        image_parts = []
        for img in images:
            image_parts.append(
                {
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": _tensor_to_base64_png(img),
                    }
                }
            )

        image_tensor, output_name = _post_generation_request(
            api_key=api_key,
            model_name=model_name,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            image_payload_parts=image_parts,
        )
        return (image_tensor, output_name, len(images))


NODE_CLASS_MAPPINGS = {
    "APIYI_Text_To_Image": APIYITextToImageNode,
    "APIYI_Multi_Image_Edit": APIYIMultiImageEditNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "APIYI_Text_To_Image": "APIYI Text to Image",
    "APIYI_Multi_Image_Edit": "APIYI Multi Image Edit (Up to 5 Images)",
}
