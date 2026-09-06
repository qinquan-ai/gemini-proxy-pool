import asyncio
import glob
import json
import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Union

AGY_PATH = r"C:\Users\Administrator\AppData\Local\agy\bin\agy.exe"
CLI_BRAIN_DIR = r"C:\Users\Administrator\.gemini\antigravity-cli\brain"


def generate_image_sync(
    prompt: str,
    image_name: str = "generated_image",
    aspect_ratio: str = "1:1",
    output_path: Optional[str] = None,
    image_paths: Optional[Union[List[str], str]] = None,
) -> Dict[str, Any]:
    """使用 AGY 引擎生成或基于参考图编辑图片。

    Args:
        prompt: 画面描述或针对参考图的修改指令。
        image_name: 图片标识名称。
        aspect_ratio: 输出画面比例 ('1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3')。
        output_path: 目标保存文件路径。
        image_paths: 参考图路径列表（最多支持 3 张，支持图生图、风格迁移、垫图修改）。
    """
    if not os.path.exists(AGY_PATH):
        return {"status": "error", "error": f"AGY CLI binary not found at {AGY_PATH}"}

    cleaned_name = (
        "".join(c if c.isalnum() or c in "_-" else "_" for c in image_name).strip("_")
        or "image"
    )

    # 处理参考图路径
    valid_image_paths: List[str] = []
    if image_paths:
        raw_list = [image_paths] if isinstance(image_paths, str) else image_paths
        for p in raw_list:
            if not p:
                continue
            abs_p = os.path.abspath(str(p).strip())
            if os.path.exists(abs_p):
                valid_image_paths.append(abs_p)
            else:
                # 兼容路径查找
                norm_p = os.path.normpath(str(p).strip())
                if os.path.exists(norm_p):
                    valid_image_paths.append(norm_p)

        valid_image_paths = valid_image_paths[:3]

    start_time = time.time()

    if valid_image_paths:
        paths_json = json.dumps(valid_image_paths, ensure_ascii=False)
        cmd_prompt = (
            f"Call generate_image tool with prompt: '{prompt}', AspectRatio: '{aspect_ratio}', "
            f"ImageName: '{cleaned_name}', ImagePaths: {paths_json}"
        )
    else:
        cmd_prompt = (
            f"Call generate_image tool with prompt: '{prompt}', AspectRatio: '{aspect_ratio}', "
            f"ImageName: '{cleaned_name}'"
        )

    cmd = [AGY_PATH, "--dangerously-skip-permissions", "-p", cmd_prompt]

    try:
        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Image generation timed out after 180s"}
    except Exception as e:
        return {"status": "error", "error": f"Execution failed: {str(e)}"}

    # 查找最近生成在 brain 目录中的图片
    pattern = os.path.join(CLI_BRAIN_DIR, "**", "*.jpg")
    found_files = glob.glob(pattern, recursive=True)

    # 过滤出在本次调用之后或相近时间内修改的文件
    recent_files = [f for f in found_files if os.path.getmtime(f) >= (start_time - 10)]
    recent_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)

    if not recent_files:
        # 降级：取全局最新的图片
        all_jpgs = sorted(found_files, key=lambda x: os.path.getmtime(x), reverse=True)
        if all_jpgs:
            recent_files = [all_jpgs[0]]

    if not recent_files:
        return {
            "status": "error",
            "error": "Image generation tool finished but no output image file was detected in brain dir.",
            "stdout": process.stdout,
            "stderr": process.stderr,
        }

    latest_image = recent_files[0]
    final_path = latest_image

    # 如果指定了输出路径，复制过去
    if output_path:
        dest = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(latest_image, dest)
        final_path = dest

    return {
        "status": "success",
        "image_path": os.path.abspath(final_path),
        "aspect_ratio": aspect_ratio,
        "prompt": prompt,
        "reference_images": valid_image_paths,
        "size_bytes": os.path.getsize(final_path),
    }


async def generate_image_async(
    prompt: str,
    image_name: str = "generated_image",
    aspect_ratio: str = "1:1",
    output_path: Optional[str] = None,
    image_paths: Optional[Union[List[str], str]] = None,
) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        generate_image_sync,
        prompt,
        image_name,
        aspect_ratio,
        output_path,
        image_paths,
    )


if __name__ == "__main__":
    test_out = (
        r"D:\project_qin\Proxy_project\StudioKey_proxy\tests\test_ref_output.jpg"
    )
    print("Testing generate_image_sync with reference image...")
    # 测试垫图 / 参考图
    ref_img = r"C:\Users\Administrator\.gemini\antigravity-ide\brain\9134fb1f-ce94-48c2-b532-a4abbc5fbb9b\cyber_cat_test.jpg"
    res = generate_image_sync(
        prompt="Transform this cat into a cute watercolor painting style with pastel colors",
        image_name="watercolor_cat",
        aspect_ratio="1:1",
        output_path=test_out,
        image_paths=[ref_img],
    )
    print("Result:", res)
