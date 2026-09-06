import asyncio
import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional, Union

AGY_PATH = r"C:\Users\Administrator\AppData\Local\agy\bin\agy.exe"


def analyze_image_sync(
    image_path: Union[str, List[str]],
    prompt: str = "请详细分析并描述这张图片的内容。",
) -> Dict[str, Any]:
    """使用本地 AGY 引擎对本地图片进行深度多模态分析。

    Args:
        image_path: 本地图片路径，支持单张路径字符串或多张图片路径列表。
        prompt: 具体分析需求（如：UI/组件拆解、OCR文字提取、错误截图诊断、设计规范推导等）。

    Returns:
        包含分析结果的字典。
    """
    if not os.path.exists(AGY_PATH):
        return {
            "status": "error",
            "error": f"AGY CLI binary not found at {AGY_PATH}",
        }

    # 处理图片路径
    paths_list = [image_path] if isinstance(image_path, str) else list(image_path)
    valid_paths: List[str] = []
    for p in paths_list:
        if not p:
            continue
        abs_p = os.path.abspath(str(p).strip())
        if os.path.exists(abs_p):
            valid_paths.append(abs_p)
        else:
            norm_p = os.path.normpath(str(p).strip())
            if os.path.exists(norm_p):
                valid_paths.append(norm_p)

    if not valid_paths:
        return {
            "status": "error",
            "error": f"No valid image files found at the specified path(s): {image_path}",
        }

    start_time = time.time()

    if len(valid_paths) == 1:
        target_path = valid_paths[0]
        cmd_prompt = (
            f"Please use the view_file tool to view the image file at '{target_path}'. "
            f"Then, answer or perform the following user analysis request in detail:\n\n{prompt}"
        )
    else:
        paths_desc = "\n".join([f"- Image {idx+1}: '{p}'" for idx, p in enumerate(valid_paths)])
        cmd_prompt = (
            f"Please use the view_file tool to view each of the following image files:\n{paths_desc}\n\n"
            f"Then, answer or perform the following user analysis request across these images:\n\n{prompt}"
        )

    cmd = [
        AGY_PATH,
        "--dangerously-skip-permissions",
        "-p",
        cmd_prompt,
    ]

    try:
        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=240,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "error": "Image analysis timed out after 240 seconds.",
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to execute AGY CLI: {str(e)}",
        }

    elapsed = round(time.time() - start_time, 2)
    output_text = (process.stdout or "").strip()

    if not output_text and process.returncode != 0:
        return {
            "status": "error",
            "error": f"AGY CLI returned exit code {process.returncode}: {process.stderr}",
            "elapsed_seconds": elapsed,
        }

    return {
        "status": "success",
        "analysis": output_text,
        "image_paths": valid_paths,
        "prompt": prompt,
        "elapsed_seconds": elapsed,
    }


async def analyze_image_async(
    image_path: Union[str, List[str]],
    prompt: str = "请详细分析并描述这张图片的内容。",
) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        analyze_image_sync,
        image_path,
        prompt,
    )


if __name__ == "__main__":
    test_img = r"C:\Users\Administrator\.gemini\antigravity-ide\brain\9134fb1f-ce94-48c2-b532-a4abbc5fbb9b\cyber_cat_test.jpg"
    print("Testing analyze_image_sync...")
    res = analyze_image_sync(
        image_path=test_img,
        prompt="简要用一两句话描述这张图片的内容与艺术风格。",
    )
    print("Result status:", res.get("status"))
    print("Elapsed seconds:", res.get("elapsed_seconds"))
    print("Analysis:\n", res.get("analysis"))
