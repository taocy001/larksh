"""
[开发调试工具] 验证飞书文档 raw_content 是否能完整保留文件内容（含空格、Tab、特殊字符、尾部换行）

此脚本用于验证 /edit 命令依赖的飞书文档 API 行为，不是运行时组件，不会被打包进分发包。

用法：
    python tools/verify_docx_fidelity.py --app-id cli_xxx --app-secret xxx
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

# ──────────────────────────────────────────────
# 测试用例：覆盖各种格式敏感内容
# ──────────────────────────────────────────────
TEST_CASES = {
    "plain_text": (
        "Hello, World!\n"
        "第二行中文\n"
        "  leading spaces\n"
        "\tleading tab\n"
        "trailing space   \n"
        "empty line below\n"
        "\n"
        "last line no newline"
    ),
    "code_like": (
        "def foo(x):\n"
        "    if x > 0:\n"
        "        return x * 2\n"
        "    # comment with # hash\n"
        '    return "string with \\"quotes\\""\n'
    ),
    "special_chars": (
        "tab:\there\n"
        "null-ish: \x00 (but URL-safe)\n"
        "backslash: \\\n"
        "angle: <tag> & 'quote'\n"
        "unicode: 中文 日本語 한국어 😀\n"
    ),
}


class FeishuDocxTester:
    TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    DOCX_CREATE = "https://open.feishu.cn/open-apis/docx/v1/documents"

    def __init__(self, app_id: str, app_secret: str):
        self._app_id = app_id
        self._app_secret = app_secret
        self._client = httpx.Client(timeout=30.0)
        self._token = self._fetch_token()

    def _fetch_token(self) -> str:
        resp = self._client.post(
            self.TOKEN_URL,
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 token 失败: {data}")
        print(f"✅ Token 获取成功，{data.get('expire', '?')}s 有效")
        return data["tenant_access_token"]

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def create_doc(self, title: str) -> str:
        """创建空文档，返回 document_id"""
        resp = self._client.post(
            self.DOCX_CREATE,
            headers=self._headers(),
            json={"title": title},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"创建文档失败: {data}")
        doc_id = data["data"]["document"]["document_id"]
        print(f"  📄 文档已创建: {doc_id}")
        return doc_id

    def append_code_block(self, doc_id: str, content: str) -> None:
        """在文档末尾追加一个 code block（language=Plain Text）"""
        url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children"
        payload = {
            "children": [
                {
                    "block_type": 14,  # code block
                    "code": {
                        "style": {"language": 1, "wrap": False},  # language=1: PlainText
                        "elements": [
                            {
                                "type": "text_run",
                                "text_run": {"content": content},
                            }
                        ],
                    },
                }
            ],
            "index": -1,
        }
        resp = self._client.post(url, headers=self._headers(), json=payload)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"追加代码块失败: {data}")

    def append_markdown_fence(self, doc_id: str, content: str, lang: str = "") -> None:
        """用 Markdown 代码围栏格式（```lang\ncontent\n```）写入文档段落"""
        fenced = f"```{lang}\n{content}\n```"
        url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children"
        payload = {
            "children": [
                {
                    "block_type": 2,  # text paragraph
                    "text": {
                        "elements": [
                            {
                                "type": "text_run",
                                "text_run": {"content": fenced},
                            }
                        ],
                        "style": {},
                    },
                }
            ],
            "index": -1,
        }
        resp = self._client.post(url, headers=self._headers(), json=payload)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"追加 Markdown 块失败: {data}")

    def get_raw_content(self, doc_id: str) -> str:
        """获取文档 raw_content"""
        url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/raw_content"
        resp = self._client.get(url, headers=self._headers())
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 raw_content 失败: {data}")
        return data["data"]["content"]

    def delete_doc(self, doc_id: str) -> None:
        """删除文档（清理）"""
        url = f"https://open.feishu.cn/open-apis/drive/v1/files/{doc_id}"
        resp = self._client.delete(
            url,
            headers=self._headers(),
            params={"type": "docx"},
        )
        if resp.status_code == 200 and resp.json().get("code") == 0:
            print(f"  🗑  文档已删除: {doc_id}")
        else:
            print(f"  ⚠️  文档删除失败（可手动删除）: {doc_id} → {resp.text[:200]}")


def run_test(tester: FeishuDocxTester, name: str, content: str, use_fence: bool) -> bool:
    mode = "Markdown fence" if use_fence else "code block"
    print(f"\n── 测试 [{name}] via {mode} ──")
    doc_id = tester.create_doc(f"larksh-verify-{name}-{int(time.time())}")
    try:
        if use_fence:
            tester.append_markdown_fence(doc_id, content)
        else:
            tester.append_code_block(doc_id, content)

        # 等 API 一致性
        time.sleep(1)
        raw = tester.get_raw_content(doc_id)

        # 分析结果
        print(f"  原始长度: {len(content)} chars")
        print(f"  raw_content 长度: {len(raw)} chars")
        print(f"  raw_content 预览（前200字符）:\n    {repr(raw[:200])}")

        if use_fence:
            # 尝试从 Markdown fence 提取内容
            extracted = _extract_from_fence(raw)
            if extracted is None:
                print("  ❌ 无法从 raw_content 提取 Markdown fence 内容")
                return False
            match = extracted == content
            print(f"  fence 提取后匹配: {'✅' if match else '❌'}")
            if not match:
                _show_diff(content, extracted)
        else:
            # code block 的 raw_content 应该直接包含内容
            match = content in raw
            print(f"  内容包含在 raw_content 中: {'✅' if match else '❌'}")
            if not match:
                print(f"  期望内容: {repr(content[:100])}")

        return match
    finally:
        tester.delete_doc(doc_id)


def _extract_from_fence(raw: str) -> str | None:
    """从 raw_content 中提取 ``` ... ``` 围栏内的内容"""
    lines = raw.split("\n")
    in_fence = False
    fence_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not in_fence and stripped.startswith("```"):
            in_fence = True
            continue
        if in_fence:
            if stripped == "```":
                return "\n".join(fence_lines)
            fence_lines.append(line)
    return None


def _show_diff(expected: str, got: str) -> None:
    exp_lines = expected.splitlines()
    got_lines = got.splitlines()
    for i, (e, g) in enumerate(zip(exp_lines, got_lines)):
        if e != g:
            print(f"  行 {i+1} 不一致:")
            print(f"    期望: {repr(e)}")
            print(f"    实际: {repr(g)}")
    if len(exp_lines) != len(got_lines):
        print(f"  行数不一致: 期望 {len(exp_lines)}, 实际 {len(got_lines)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--app-secret", required=True)
    parser.add_argument(
        "--mode",
        choices=["code_block", "fence", "both"],
        default="both",
        help="测试写入方式",
    )
    args = parser.parse_args()

    tester = FeishuDocxTester(args.app_id, args.app_secret)

    results: list[bool] = []
    for name, content in TEST_CASES.items():
        if args.mode in ("code_block", "both"):
            results.append(run_test(tester, name, content, use_fence=False))
        if args.mode in ("fence", "both"):
            results.append(run_test(tester, f"{name}_fence", content, use_fence=True))

    print("\n══════════════════════════════")
    passed = sum(results)
    total = len(results)
    print(f"结果: {passed}/{total} 通过")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
