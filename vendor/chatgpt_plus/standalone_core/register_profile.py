"""浏览器指纹元数据（UA / SecChUA / Locale / Timezone）。

每个注册任务生成一份 Profile，并在整条 OAuth/Sentinel 链路上保持一致。
优先按代理 region（或出口 IP 国家）对齐语言与时区，降低 IP-指纹错配风险。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Pick one exact bundled TLS/HTTP2 preset per account and keep it sticky for
# that account's complete registration/OAuth transaction.
# Protocol registration must advertise the exact browser major implemented by
# both installed TLS stacks.  Hardware/locale/persona diversity stays broad;
# inventing Chrome 148/150 above a chrome_146 handshake is not diversity, it is
# a cross-layer contradiction.
_REALISTIC_MAJORS: tuple[int, ...] = (145, 146)
_WINDOWS_UA_PLATFORM = "Windows NT 10.0; Win64; x64"
_MAC_UA_PLATFORMS: tuple[str, ...] = (
    "Macintosh; Intel Mac OS X 10_15_7",
    "Macintosh; Intel Mac OS X 13_6_7",
    "Macintosh; Intel Mac OS X 14_5",
)

# Accept-Language, navigator.language
_LOCALES_GENERIC: tuple[tuple[str, str], ...] = (
    ("en-US,en;q=0.9", "en-US"),
    ("en-GB,en;q=0.9", "en-GB"),
    ("en-US,en;q=0.9,zh-CN;q=0.8", "en-US"),
)

_LOCALES_BY_REGION: dict[str, tuple[tuple[str, str], ...]] = {
    "US": (("en-US,en;q=0.9", "en-US"), ("en-US,en;q=0.9,es;q=0.7", "en-US")),
    "CA": (("en-CA,en;q=0.9", "en-CA"), ("en-US,en;q=0.9", "en-US")),
    "GB": (("en-GB,en;q=0.9", "en-GB"), ("en-GB,en;q=0.9,en-US;q=0.8", "en-GB")),
    "UK": (("en-GB,en;q=0.9", "en-GB"),),
    "IE": (("en-IE,en;q=0.9", "en-IE"), ("en-GB,en;q=0.9", "en-GB")),
    "AU": (("en-AU,en;q=0.9", "en-AU"), ("en-US,en;q=0.9", "en-US")),
    "NZ": (("en-NZ,en;q=0.9", "en-NZ"), ("en-AU,en;q=0.9", "en-AU")),
    "DE": (("de-DE,de;q=0.9,en;q=0.8", "de-DE"), ("en-US,en;q=0.9", "en-US")),
    "FR": (("fr-FR,fr;q=0.9,en;q=0.8", "fr-FR"), ("en-US,en;q=0.9", "en-US")),
    "NL": (("nl-NL,nl;q=0.9,en;q=0.8", "nl-NL"), ("en-US,en;q=0.9", "en-US")),
    "IT": (("it-IT,it;q=0.9,en;q=0.8", "it-IT"), ("en-US,en;q=0.9", "en-US")),
    "ES": (("es-ES,es;q=0.9,en;q=0.8", "es-ES"), ("en-US,en;q=0.9", "en-US")),
    "PT": (("pt-PT,pt;q=0.9,en;q=0.8", "pt-PT"), ("en-US,en;q=0.9", "en-US")),
    "BR": (("pt-BR,pt;q=0.9,en;q=0.8", "pt-BR"), ("en-US,en;q=0.9", "en-US")),
    "JP": (("ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7", "ja-JP"),),
    "KR": (("ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7", "ko-KR"),),
    "SG": (("en-SG,en;q=0.9", "en-SG"), ("en-US,en;q=0.9,zh-CN;q=0.7", "en-US")),
    "HK": (("zh-HK,zh;q=0.9,en;q=0.8", "zh-HK"), ("en-HK,en;q=0.9", "en-HK")),
    "TW": (("zh-TW,zh;q=0.9,en;q=0.8", "zh-TW"), ("en-US,en;q=0.9", "en-US")),
    "MY": (("en-MY,en;q=0.9", "en-MY"), ("ms-MY,ms;q=0.9,en;q=0.8", "ms-MY")),
    "PH": (("en-PH,en;q=0.9", "en-PH"), ("en-US,en;q=0.9", "en-US")),
    "TH": (("th-TH,th;q=0.9,en;q=0.8", "th-TH"), ("en-US,en;q=0.9", "en-US")),
    "VN": (("vi-VN,vi;q=0.9,en;q=0.8", "vi-VN"), ("en-US,en;q=0.9", "en-US")),
    "ID": (("id-ID,id;q=0.9,en;q=0.8", "id-ID"), ("en-US,en;q=0.9", "en-US")),
    "IN": (("en-IN,en;q=0.9", "en-IN"), ("hi-IN,hi;q=0.9,en;q=0.8", "hi-IN")),
    "TR": (("tr-TR,tr;q=0.9,en;q=0.8", "tr-TR"), ("en-US,en;q=0.9", "en-US")),
    "AE": (("ar-AE,ar;q=0.9,en;q=0.8", "ar-AE"), ("en-US,en;q=0.9", "en-US")),
    "SA": (("ar-SA,ar;q=0.9,en;q=0.8", "ar-SA"), ("en-US,en;q=0.9", "en-US")),
    "MX": (("es-MX,es;q=0.9,en;q=0.8", "es-MX"), ("en-US,en;q=0.9", "en-US")),
    "AR": (("es-AR,es;q=0.9,en;q=0.8", "es-AR"), ("en-US,en;q=0.9", "en-US")),
    "CL": (("es-CL,es;q=0.9,en;q=0.8", "es-CL"), ("en-US,en;q=0.9", "en-US")),
    "CO": (("es-CO,es;q=0.9,en;q=0.8", "es-CO"), ("en-US,en;q=0.9", "en-US")),
}

_HISTORY_LENGTHS: tuple[int, ...] = (2, 3, 4, 5, 6, 7, 8, 10)

# Hardware values are sampled as a persona instead of independently.  This
# avoids impossible/rare joins such as a 1366x768 desktop, 32 GB RAM, 4 cores
# and ten touch points.  Consumers may pin a persona for a sticky account.
_HARDWARE_PERSONAS: dict[str, dict[str, object]] = {
    "win_mainstream": {
        "weight": 14,
        "platform": "Windows",
        "resolutions": ("1920x1080", "1536x864", "1600x900", "1366x768"),
        "cores": (8, 12, 16),
        "memory": (8.0, 16.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("10.0.0", "15.0.0", "19.0.0"),
        "webgl": (
            (
                "Google Inc. (Intel)",
                "ANGLE (Intel, Intel(R) UHD Graphics 630, Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ),
            (
                "Google Inc. (NVIDIA)",
                "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650, Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ),
        ),
    },
    "win_gaming": {
        "weight": 8,
        "platform": "Windows",
        "resolutions": ("1920x1080", "2560x1440", "3440x1440"),
        "cores": (12, 16, 20, 24),
        "memory": (16.0, 32.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("15.0.0", "19.0.0"),
        "webgl": (
            (
                "Google Inc. (NVIDIA)",
                "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER, Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ),
            (
                "Google Inc. (NVIDIA)",
                "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060, Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ),
            (
                "Google Inc. (AMD)",
                "ANGLE (AMD, AMD Radeon RX 6600 XT, Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ),
        ),
    },
    "win_gtx1660": {
        "weight": 1,
        "platform": "Windows",
        "resolutions": ("1920x1080",),
        "cores": (20,),
        "memory": (8.0,),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("15.0.0",),
        "webgl": (
            (
                "Google Inc. (NVIDIA)",
                "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER, Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ),
        ),
    },
    "win_touch_laptop": {
        "weight": 3,
        "platform": "Windows",
        "resolutions": ("1920x1080", "1536x864", "1280x800"),
        "cores": (8, 12, 16),
        "memory": (8.0, 16.0),
        "touch": (10,),
        "architecture": "x86",
        "platform_versions": ("15.0.0", "19.0.0"),
        "webgl": (
            (
                "Google Inc. (Intel)",
                "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ),
        ),
    },
    "mac_intel": {
        "weight": 5,
        "platform": "macOS",
        "resolutions": ("1440x900", "1680x1050", "2560x1600"),
        "cores": (8, 12),
        "memory": (8.0, 16.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("13.6.0", "14.5.0"),
        "webgl": (
            (
                "Google Inc. (Intel Inc.)",
                "ANGLE (Intel Inc., Intel(R) Iris(TM) Plus Graphics, OpenGL 4.1)",
            ),
        ),
    },
    "win_office_uhd630": {
        "weight": 10,
        "platform": "Windows",
        "resolutions": ("1920x1080", "1600x900", "1366x768"),
        "cores": (4, 8),
        "memory": (8.0, 16.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("10.0.0", "15.0.0"),
        "webgl": ((
            "Google Inc. (Intel)",
            "ANGLE (Intel, Intel(R) UHD Graphics 630, Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
    "win_office_vega8": {
        "weight": 7,
        "platform": "Windows",
        "resolutions": ("1920x1080", "1600x900", "1366x768"),
        "cores": (8, 12),
        "memory": (8.0, 16.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("10.0.0", "15.0.0"),
        "webgl": ((
            "Google Inc. (AMD)",
            "ANGLE (AMD, AMD Radeon(TM) Vega 8 Graphics, Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
    "win_laptop_iris_xe": {
        "weight": 12,
        "platform": "Windows",
        "resolutions": ("1920x1080", "1920x1200", "1536x864", "1366x768"),
        "cores": (8, 12, 16),
        "memory": (8.0, 16.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("15.0.0", "19.0.0"),
        "webgl": ((
            "Google Inc. (Intel)",
            "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
    "win_laptop_amd_780m": {
        "weight": 6,
        "platform": "Windows",
        "resolutions": ("1920x1080", "1920x1200", "2560x1600"),
        "cores": (12, 16),
        "memory": (16.0, 32.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("15.0.0", "19.0.0"),
        "webgl": ((
            "Google Inc. (AMD)",
            "ANGLE (AMD, AMD Radeon 780M Graphics, Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
    "win_gaming_rtx2060": {
        "weight": 4,
        "platform": "Windows",
        "resolutions": ("1920x1080", "2560x1440"),
        "cores": (12, 16),
        "memory": (16.0, 32.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("10.0.0", "15.0.0"),
        "webgl": ((
            "Google Inc. (NVIDIA)",
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060, Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
    "win_gaming_rtx3060": {
        "weight": 8,
        "platform": "Windows",
        "resolutions": ("1920x1080", "2560x1440"),
        "cores": (12, 16, 20),
        "memory": (16.0, 32.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("15.0.0", "19.0.0"),
        "webgl": ((
            "Google Inc. (NVIDIA)",
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060, Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
    "win_gaming_rtx3070": {
        "weight": 4,
        "platform": "Windows",
        "resolutions": ("1920x1080", "2560x1440", "3440x1440"),
        "cores": (16, 20, 24),
        "memory": (16.0, 32.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("15.0.0", "19.0.0"),
        "webgl": ((
            "Google Inc. (NVIDIA)",
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070, Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
    "win_gaming_rtx4060": {
        "weight": 7,
        "platform": "Windows",
        "resolutions": ("1920x1080", "2560x1440"),
        "cores": (16, 20, 24),
        "memory": (16.0, 32.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("15.0.0", "19.0.0"),
        "webgl": ((
            "Google Inc. (NVIDIA)",
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060, Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
    "win_gaming_rtx4070": {
        "weight": 3,
        "platform": "Windows",
        "resolutions": ("2560x1440", "3440x1440", "3840x2160"),
        "cores": (20, 24, 32),
        "memory": (32.0, 64.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("19.0.0",),
        "webgl": ((
            "Google Inc. (NVIDIA)",
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070, Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
    "win_gaming_rx6600": {
        "weight": 5,
        "platform": "Windows",
        "resolutions": ("1920x1080", "2560x1440"),
        "cores": (12, 16),
        "memory": (16.0, 32.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("15.0.0", "19.0.0"),
        "webgl": ((
            "Google Inc. (AMD)",
            "ANGLE (AMD, AMD Radeon RX 6600 XT, Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
    "win_gaming_rx6700xt": {
        "weight": 3,
        "platform": "Windows",
        "resolutions": ("1920x1080", "2560x1440", "3440x1440"),
        "cores": (16, 20, 24),
        "memory": (16.0, 32.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("15.0.0", "19.0.0"),
        "webgl": ((
            "Google Inc. (AMD)",
            "ANGLE (AMD, AMD Radeon RX 6700 XT, Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
    "win_workstation_a2000": {
        "weight": 1,
        "platform": "Windows",
        "resolutions": ("1920x1080", "2560x1440", "3840x2160"),
        "cores": (16, 24, 32),
        "memory": (32.0, 64.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("15.0.0", "19.0.0"),
        "webgl": ((
            "Google Inc. (NVIDIA)",
            "ANGLE (NVIDIA, NVIDIA RTX A2000, Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
    "win_workstation_a4000": {
        "weight": 1,
        "platform": "Windows",
        "resolutions": ("2560x1440", "3840x2160"),
        "cores": (24, 32, 64),
        "memory": (32.0, 64.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("19.0.0",),
        "webgl": ((
            "Google Inc. (NVIDIA)",
            "ANGLE (NVIDIA, NVIDIA RTX A4000, Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
    "win_low_power_uhd620": {
        "weight": 7,
        "platform": "Windows",
        "resolutions": ("1366x768", "1536x864", "1920x1080"),
        "cores": (4, 8),
        "memory": (4.0, 8.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("10.0.0", "15.0.0"),
        "webgl": ((
            "Google Inc. (Intel)",
            "ANGLE (Intel, Intel(R) UHD Graphics 620, Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
    "win_ultrawide_rtx3080": {
        "weight": 1,
        "platform": "Windows",
        "resolutions": ("3440x1440", "3840x1600"),
        "cores": (20, 24, 32),
        "memory": (32.0, 64.0),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("15.0.0", "19.0.0"),
        "webgl": ((
            "Google Inc. (NVIDIA)",
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080, Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
    "win_touch_iris_xe": {
        "weight": 4,
        "platform": "Windows",
        "resolutions": ("1920x1080", "1920x1200", "2256x1504"),
        "cores": (8, 12, 16),
        "memory": (8.0, 16.0),
        "touch": (10,),
        "architecture": "x86",
        "platform_versions": ("15.0.0", "19.0.0"),
        "webgl": ((
            "Google Inc. (Intel)",
            "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
    "mac_m1": {
        "weight": 8,
        "platform": "macOS",
        "resolutions": ("1440x900", "1680x1050", "2560x1600"),
        "cores": (8,),
        "memory": (8.0, 16.0),
        "touch": (0,),
        "architecture": "arm",
        "platform_versions": ("13.6.0", "14.5.0"),
        "webgl": ((
            "Google Inc. (Apple)",
            "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)",
        ),),
    },
    "mac_m2": {
        "weight": 7,
        "platform": "macOS",
        "resolutions": ("1470x956", "1710x1112", "2560x1664", "3024x1964"),
        "cores": (8, 10, 12),
        "memory": (8.0, 16.0, 24.0),
        "touch": (0,),
        "architecture": "arm",
        "platform_versions": ("13.6.0", "14.5.0", "15.0.0"),
        "webgl": ((
            "Google Inc. (Apple)",
            "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
        ),),
    },
    "mac_m3": {
        "weight": 5,
        "platform": "macOS",
        "resolutions": ("1470x956", "1710x1112", "2560x1664", "3024x1964"),
        "cores": (8, 10, 12),
        "memory": (8.0, 16.0, 24.0, 36.0),
        "touch": (0,),
        "architecture": "arm",
        "platform_versions": ("14.5.0", "15.0.0"),
        "webgl": ((
            "Google Inc. (Apple)",
            "ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)",
        ),),
    },
    "mac_m4": {
        "weight": 3,
        "platform": "macOS",
        "resolutions": ("1470x956", "1710x1112", "2560x1664", "3024x1964"),
        "cores": (10, 12, 14),
        "memory": (16.0, 24.0, 32.0),
        "touch": (0,),
        "architecture": "arm",
        "platform_versions": ("15.0.0",),
        "webgl": ((
            "Google Inc. (Apple)",
            "ANGLE (Apple, ANGLE Metal Renderer: Apple M4, Unspecified Version)",
        ),),
    },
    "mac_intel_iris_pro": {
        "weight": 2,
        "platform": "macOS",
        "resolutions": ("1920x1080", "2560x1440", "2560x1600"),
        "cores": (16,),
        "memory": (8.0,),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("13.6.0", "14.5.0"),
        "webgl": ((
            "Google Inc. (Intel Inc.)",
            "ANGLE (Intel, ANGLE Metal Renderer: Intel Iris Pro OpenGL Engine, Unspecified Version)",
        ),),
    },
    "win_amd_radeon_164e": {
        "weight": 2,
        "platform": "Windows",
        "resolutions": ("1920x1080", "1920x1200", "2560x1440"),
        "cores": (20,),
        "memory": (8.0,),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("15.0.0", "19.0.0"),
        "webgl": ((
            "Google Inc. (AMD)",
            "ANGLE (AMD, AMD Radeon(TM) Graphics (0x0000164E) Direct3D11 vs_5_0 ps_5_0, D3D11-27.20.11028.10001)",
        ),),
    },
    "win_amd_760g": {
        "weight": 1,
        "platform": "Windows",
        "resolutions": ("1366x768", "1600x900", "1920x1080"),
        "cores": (20,),
        "memory": (8.0,),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("10.0.0",),
        "webgl": ((
            "Google Inc. (AMD)",
            "ANGLE (AMD, AMD 760G (Microsoft Corporation WDDM 1.1) (0x00001636) Direct3D9Ex vs_3_0 ps_3_0, D3D9Ex)",
        ),),
    },
    "win_intel_uhd600": {
        "weight": 2,
        "platform": "Windows",
        "resolutions": ("1366x768", "1600x900", "1920x1080"),
        "cores": (20,),
        "memory": (8.0,),
        "touch": (0,),
        "architecture": "x86",
        "platform_versions": ("10.0.0", "15.0.0"),
        "webgl": ((
            "Google Inc. (Intel)",
            "ANGLE (Intel, Intel(R) UHD Graphics 600 (0x00003185) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),),
    },
}

# (offset_min, label) — multiple options per region to avoid fixed fingerprint
_TIMEZONES_BY_REGION: dict[str, tuple[tuple[int, str], ...]] = {
    "JP": ((540, "日本标准时间"),),
    "KR": ((540, "韩国标准时间"),),
    "SG": ((480, "新加坡标准时间"),),
    "HK": ((480, "香港标准时间"),),
    "TW": ((480, "台北标准时间"),),
    "MY": ((480, "马来西亚时间"),),
    "PH": ((480, "菲律宾标准时间"),),
    "CN": ((480, "中国标准时间"),),
    "TH": ((420, "印度支那时间"),),
    "VN": ((420, "印度支那时间"),),
    "ID": ((420, "印度尼西亚西部时间"), (480, "印度尼西亚中部时间"), (540, "印度尼西亚东部时间")),
    "IN": ((330, "印度标准时间"),),
    "AE": ((240, "海湾标准时间"),),
    "SA": ((180, "阿拉伯标准时间"),),
    "TR": ((180, "土耳其时间"),),
    "US": (
        (-480, "太平洋标准时间"),
        (-420, "太平洋夏令时间"),
        (-420, "山地标准时间"),
        (-360, "山地夏令时间"),
        (-360, "中部标准时间"),
        (-300, "中部夏令时间"),
        (-300, "东部标准时间"),
        (-240, "东部夏令时间"),
    ),
    "CA": (
        (-480, "太平洋标准时间"),
        (-420, "太平洋夏令时间"),
        (-300, "东部标准时间"),
        (-240, "东部夏令时间"),
    ),
    "GB": ((0, "格林尼治标准时间"), (60, "英国夏令时间")),
    "UK": ((0, "格林尼治标准时间"), (60, "英国夏令时间")),
    "IE": ((0, "格林尼治标准时间"), (60, "爱尔兰夏令时间")),
    "DE": ((60, "中欧标准时间"), (120, "中欧夏令时间")),
    "FR": ((60, "中欧标准时间"), (120, "中欧夏令时间")),
    "NL": ((60, "中欧标准时间"), (120, "中欧夏令时间")),
    "IT": ((60, "中欧标准时间"), (120, "中欧夏令时间")),
    "ES": ((60, "中欧标准时间"), (120, "中欧夏令时间")),
    "PT": ((0, "西欧标准时间"), (60, "西欧夏令时间")),
    "AU": ((600, "澳大利亚东部标准时间"), (660, "澳大利亚东部夏令时间"), (570, "澳大利亚中部标准时间")),
    "NZ": ((720, "新西兰标准时间"), (780, "新西兰夏令时间")),
    "BR": ((-180, "巴西利亚时间"), (-120, "亚马逊时间")),
    "MX": ((-360, "中部标准时间"), (-300, "中部夏令时间"), (-420, "太平洋标准时间")),
    "AR": ((-180, "阿根廷时间"),),
    "CL": ((-240, "智利标准时间"), (-180, "智利夏令时间")),
    "CO": ((-300, "哥伦比亚时间"),),
}

# default fallback pool
_TIMEZONES_DEFAULT: tuple[tuple[int, str], ...] = (
    (540, "日本标准时间"),
    (480, "中国标准时间"),
    (-300, "东部标准时间"),
    (60, "中欧夏令时间"),
    (0, "格林尼治标准时间"),
)

# IANA names for logging / optional consumers
_IANA_BY_REGION: dict[str, tuple[str, ...]] = {
    "JP": ("Asia/Tokyo",),
    "KR": ("Asia/Seoul",),
    "SG": ("Asia/Singapore",),
    "HK": ("Asia/Hong_Kong",),
    "TW": ("Asia/Taipei",),
    "MY": ("Asia/Kuala_Lumpur",),
    "PH": ("Asia/Manila",),
    "CN": ("Asia/Shanghai",),
    "TH": ("Asia/Bangkok",),
    "VN": ("Asia/Ho_Chi_Minh",),
    "ID": ("Asia/Jakarta", "Asia/Makassar", "Asia/Jayapura"),
    "IN": ("Asia/Kolkata",),
    "AE": ("Asia/Dubai",),
    "SA": ("Asia/Riyadh",),
    "TR": ("Europe/Istanbul",),
    "US": ("America/Los_Angeles", "America/Denver", "America/Chicago", "America/New_York"),
    "CA": ("America/Vancouver", "America/Toronto"),
    "GB": ("Europe/London",),
    "UK": ("Europe/London",),
    "IE": ("Europe/Dublin",),
    "DE": ("Europe/Berlin",),
    "FR": ("Europe/Paris",),
    "NL": ("Europe/Amsterdam",),
    "IT": ("Europe/Rome",),
    "ES": ("Europe/Madrid",),
    "PT": ("Europe/Lisbon",),
    "AU": ("Australia/Sydney", "Australia/Melbourne", "Australia/Adelaide"),
    "NZ": ("Pacific/Auckland",),
    "BR": ("America/Sao_Paulo",),
    "MX": ("America/Mexico_City", "America/Tijuana"),
    "AR": ("America/Argentina/Buenos_Aires",),
    "CL": ("America/Santiago",),
    "CO": ("America/Bogota",),
}


@dataclass(slots=True)
class Profile:
    user_agent: str
    sec_ch_ua: str
    sec_ch_ua_platform: str
    locale: str
    language: str = "en-US"
    resolution: str = "1920x1080"
    cores: int = 8
    history_len: int = 4
    timezone_offset_min: int = 540
    timezone_label: str = "日本标准时间"
    # extra fingerprint fields (safe defaults keep old callers working)
    platform_os: str = "Windows"
    browser: str = "chrome"
    chrome_major: int = 146
    chrome_full_version: str = "146.0.7680.0"
    device_memory: float = 8.0
    max_touch_points: int = 0
    region: str = ""
    timezone_id: str = "Asia/Tokyo"
    tls_impersonate: str = "chrome146"
    tls_client_identifier: str = "chrome_146"
    # High-entropy UA-CH/runtime fields.  Firefox deliberately leaves UA-CH
    # brand fields empty because native Firefox does not emit Chromium hints.
    platform_version: str = "15.0.0"
    architecture: str = "x86"
    bitness: str = "64"
    mobile: bool = False
    hardware_profile: str = "win_mainstream"
    webgl_vendor: str = ""
    webgl_renderer: str = ""
    webrtc_mode: str = "disabled"
    geolocation_mode: str = "ask"
    canvas_mode: str = "real"
    webgl_mode: str = "real"
    audio_mode: str = "real"
    audio_seed: str = ""
    media_devices_mode: str = "real"
    media_devices_seed: str = ""
    client_rects_mode: str = "real"
    client_rects_seed: str = ""
    speech_voices_mode: str = "real"
    speech_voices_seed: str = ""
    webgpu_mode: str = "native"
    interface_language_mode: str = "language"
    do_not_track_mode: str = "default"
    port_scan_protection: bool = False
    tls_features_disabled: bool = False
    device_name: str = ""
    mac_address: str = ""
    hardware_acceleration: str = "default"

    @property
    def browser_major(self) -> int:
        """Browser-neutral alias; ``chrome_major`` remains for compatibility."""
        return self.chrome_major

    @property
    def browser_full_version(self) -> str:
        """Browser-neutral alias; ``chrome_full_version`` remains compatible."""
        return self.chrome_full_version

    def request_headers(self, *, high_entropy: bool = False) -> dict[str, str]:
        """Return internally consistent UA/locale headers for this profile."""
        return profile_headers(self, high_entropy=high_entropy)


def _rand_choice(seq):
    return seq[secrets.randbelow(len(seq))]


def _rand_int(a: int, b: int) -> int:
    return a + secrets.randbelow(b - a + 1)


def _derived_seed(seed: str, label: str) -> str:
    source = f"{str(seed or '0').upper()}:{label}".encode("utf-8")
    return hashlib.blake2s(source, digest_size=4).hexdigest().upper()


def _device_identity(platform_os: str, seed: str) -> tuple[str, str]:
    digest = hashlib.blake2s(
        f"{platform_os}:{seed}".encode("utf-8"), digest_size=8
    ).digest()
    suffix = "".join("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"[b % 36] for b in digest[:7])
    if platform_os == "macOS":
        device_name = f"iMac-{suffix[:5]}"
    else:
        prefix = ("DESKTOP", "LAPTOP", "USER-PC")[digest[7] % 3]
        device_name = f"{prefix}-{suffix}"
    mac = bytearray(digest[:6])
    mac[0] = (mac[0] | 0x02) & 0xFE  # locally administered, unicast
    separator = ":" if platform_os == "macOS" else "-"
    return device_name, separator.join(f"{part:02X}" for part in mac)


def _pick_hardware(platform_os: str, name: str | None = None) -> tuple[str, dict[str, object]]:
    requested = str(name or "").strip().lower()
    if requested:
        persona = _HARDWARE_PERSONAS.get(requested)
        if persona is None:
            raise ValueError(f"unknown hardware profile: {name}")
        if persona["platform"] != platform_os:
            raise ValueError(
                f"hardware profile {requested!r} is {persona['platform']}, not {platform_os}"
            )
        return requested, persona
    names = [
        key
        for key, value in _HARDWARE_PERSONAS.items()
        if value["platform"] == platform_os
    ]
    total = sum(max(1, int(_HARDWARE_PERSONAS[key].get("weight", 1))) for key in names)
    ticket = secrets.randbelow(total)
    selected = names[-1]
    for key in names:
        ticket -= max(1, int(_HARDWARE_PERSONAS[key].get("weight", 1)))
        if ticket < 0:
            selected = key
            break
    return selected, _HARDWARE_PERSONAS[selected]


def hardware_profile_names(platform_os: str | None = None) -> tuple[str, ...]:
    """List stable hardware persona identifiers exposed by the library."""
    wanted = str(platform_os or "").strip()
    return tuple(
        key
        for key, value in _HARDWARE_PERSONAS.items()
        if not wanted or value["platform"] == wanted
    )


def _hardware_values(persona: dict[str, object]) -> tuple[str, int, float, int, str, str, str, str]:
    resolution = _rand_choice(persona["resolutions"])
    cores = int(_rand_choice(persona["cores"]))
    memory = float(_rand_choice(persona["memory"]))
    touch = int(_rand_choice(persona["touch"]))
    platform_version = str(_rand_choice(persona["platform_versions"]))
    architecture = str(persona["architecture"])
    vendor, renderer = _rand_choice(persona["webgl"])
    return (
        str(resolution),
        cores,
        memory,
        touch,
        platform_version,
        architecture,
        str(vendor),
        str(renderer),
    )


def _chrome_full_version(major: int) -> str:
    # Known Chromium branch bases; the reduced HTTP UA still uses x.0.0.0,
    # while UAData may expose this task-stable full version.
    branch = {
        144: 7559,
        145: 7632,
        146: 7680,
        148: 7778,
        149: 7827,
        150: 7871,
    }.get(major, 7680)
    build = branch
    patch = _rand_int(60, 220)
    return f"{major}.0.{build}.{patch}"


def _build_ua_and_ch(major: int, browser: str, platform_os: str) -> tuple[str, str, str, str, str]:
    full = _chrome_full_version(major)
    if platform_os == "macOS":
        ua_plat = _rand_choice(_MAC_UA_PLATFORMS)
        ch_platform = '"macOS"'
        base = (
            f"Mozilla/5.0 ({ua_plat}) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        )
    else:
        ch_platform = '"Windows"'
        base = (
            f"Mozilla/5.0 ({_WINDOWS_UA_PLATFORM}) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        )

    if browser == "edge":
        # Edge mainly Windows; if mac selected, still emit Edge UA form
        user_agent = f"{base} Edg/{major}.0.0.0"
        sec_ch_ua = (
            f'"Not;A=Brand";v="8", "Chromium";v="{major}", '
            f'"Microsoft Edge";v="{major}"'
        )
        browser_name = "edge"
    else:
        user_agent = base
        sec_ch_ua = (
            f'"Not;A=Brand";v="8", "Chromium";v="{major}", '
            f'"Google Chrome";v="{major}"'
        )
        browser_name = "chrome"
    return user_agent, sec_ch_ua, ch_platform, full, browser_name


def proxy_region_code(proxy: str | None) -> str:
    """Extract provider region code from URLs like ...-region-JP-sid-..."""
    if not proxy:
        return ""
    text = str(proxy)
    patterns = (
        r"(?:^|[-_./?&])region[-_=/]?([A-Za-z]{2})(?:[-_./?&]|$)",
        r"(?:^|[-_./?&])(?:country|cc|loc)[-_=/]?([A-Za-z]{2})(?:[-_./?&]|$)",
    )
    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            return m.group(1).upper()
    return ""


def _pick_locale(region: str) -> tuple[str, str]:
    region = (region or "").upper()
    pool = _LOCALES_BY_REGION.get(region) or _LOCALES_GENERIC
    return _rand_choice(pool)


def _timezone_parts(timezone_id: str, region: str = "") -> tuple[int, str, str]:
    timezone_id = str(timezone_id or "UTC").strip() or "UTC"
    try:
        now = datetime.now(ZoneInfo(timezone_id))
        offset = int((now.utcoffset() or now.astimezone().utcoffset()).total_seconds() // 60)
        # Locale-neutral and DST-correct.  The same label is injected into the
        # Sentinel JS context, so requirements p and enforcement t agree.
        label = str(now.tzname() or timezone_id)
        return offset, label, timezone_id
    except (ZoneInfoNotFoundError, OSError, ValueError):
        # Minimal compatibility fallback for environments missing tzdata.
        # Production requirements install tzdata, so this branch is mainly for
        # old embedded runtimes.
        tz_pool = _TIMEZONES_BY_REGION.get((region or "").upper()) or _TIMEZONES_DEFAULT
        offset, label = _rand_choice(tz_pool)
        return offset, label, timezone_id


def _pick_timezone(region: str) -> tuple[int, str, str]:
    region = (region or "").upper()
    iana_pool = _IANA_BY_REGION.get(region) or ("UTC",)
    return _timezone_parts(_rand_choice(iana_pool), region)



# curl_cffi 0.15 request-time-supported desktop Chrome targets.
# Session construction is lazy, so unsupported values can appear valid until
# the first request. chrome144 is deliberately absent: the bundled transport
# raises "Impersonating chrome144 is not supported" at request time.
_TLS_CHROME_AVAILABLE: tuple[int, ...] = (120, 123, 124, 131, 136, 142, 145, 146)
_TLS_CHROME_SPECIAL: dict[int, str] = {
    # package uses chrome133a, not chrome133
    133: "chrome133a",
}
_TLS_FIREFOX_AVAILABLE: tuple[int, ...] = (133, 135, 144, 147)
_TLS_CLIENT_CHROME_AVAILABLE: tuple[int, ...] = (144, 146)
_TLS_CLIENT_FIREFOX_AVAILABLE: tuple[int, ...] = (146,)


def choose_tls_impersonate(
    *,
    browser: str,
    chrome_major: int,
    platform_os: str = "Windows",
) -> str:
    """Map UA browser/version to a real curl_cffi impersonate target.

    Prefer nearest lower-or-equal Chrome major so JA3 stays consistent with
    advertised Chromium version. Edge sometimes uses edge101 for diversity.
    Always return a target known-supported by curl_cffi 0.15.
    """
    browser = (browser or "chrome").lower()
    major = int(chrome_major or 146)

    if browser == "firefox":
        lower = [m for m in _TLS_FIREFOX_AVAILABLE if m <= major]
        pick = lower[-1] if lower else _TLS_FIREFOX_AVAILABLE[0]
        return f"firefox{pick}"
    if browser == "edge":
        # edge impersonate set is sparse; mix edge101 with chrome TLS
        if platform_os == "Windows" and (major % 5 == 0):
            return "edge101"
    if browser == "safari":
        return "safari18_0" if major >= 140 else "safari17_0"

    if major in _TLS_CHROME_SPECIAL:
        return _TLS_CHROME_SPECIAL[major]

    lower = [m for m in _TLS_CHROME_AVAILABLE if m <= major]
    if lower:
        pick = lower[-1]
    else:
        pick = min(_TLS_CHROME_AVAILABLE, key=lambda m: abs(m - major))
    return f"chrome{pick}"


def choose_tls_client_identifier(*, browser: str, browser_major: int) -> str:
    """Map a browser UA to an installed tls-client-python identifier."""
    browser = str(browser or "chrome").lower()
    major = int(browser_major or 146)
    if browser == "firefox":
        lower = [m for m in _TLS_CLIENT_FIREFOX_AVAILABLE if m <= major]
        pick = lower[-1] if lower else _TLS_CLIENT_FIREFOX_AVAILABLE[0]
        # The installed transport exposes Firefox 146 as a PSK preset.
        return f"firefox_{pick}_PSK"
    lower = [m for m in _TLS_CLIENT_CHROME_AVAILABLE if m <= major]
    pick = lower[-1] if lower else _TLS_CLIENT_CHROME_AVAILABLE[0]
    return f"chrome_{pick}"


# Region-localized given/family names for create_account payloads.
_NAMES_EN: tuple[tuple[str, ...], tuple[str, ...]] = (
    (
        "James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph",
        "Thomas", "Charles", "Daniel", "Matthew", "Anthony", "Mark", "Steven", "Paul",
        "Andrew", "Joshua", "Kevin", "Brian", "Emily", "Jessica", "Ashley", "Sarah",
        "Amanda", "Melissa", "Nicole", "Michelle", "Kimberly", "Amy", "Angela", "Stephanie",
        "Rebecca", "Laura", "Samantha", "Rachel", "Lauren", "Hannah", "Olivia", "Emma",
    ),
    (
        "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
        "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
        "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
        "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
        "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
    ),
)

_NAMES_BY_REGION: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "US": _NAMES_EN,
    "CA": _NAMES_EN,
    "GB": (
        (
            "Oliver", "George", "Harry", "Jack", "Jacob", "Noah", "Charlie", "Thomas",
            "Oscar", "William", "James", "Amelia", "Olivia", "Isla", "Ava", "Emily",
            "Sophia", "Grace", "Mia", "Poppy", "Lily", "Ella",
        ),
        (
            "Smith", "Jones", "Taylor", "Brown", "Williams", "Wilson", "Johnson", "Davies",
            "Patel", "Wright", "Evans", "Thomas", "Roberts", "Walker", "Thompson", "White",
            "Hughes", "Edwards", "Green", "Hall", "Clarke", "Harris",
        ),
    ),
    "AU": _NAMES_EN,
    "NZ": _NAMES_EN,
    "DE": (
        (
            "Lukas", "Leon", "Luca", "Felix", "Maximilian", "Paul", "Elias", "Jonas",
            "Noah", "Ben", "Mia", "Emma", "Hannah", "Sophia", "Emilia", "Lina", "Marie",
            "Anna", "Lea", "Lena", "Clara", "Laura",
        ),
        (
            "Muller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner",
            "Becker", "Schulz", "Hoffmann", "Schafer", "Koch", "Bauer", "Richter",
            "Klein", "Wolf", "Schroder", "Neumann", "Schwarz", "Zimmermann", "Braun",
            "Kruger", "Hofmann", "Hartmann",
        ),
    ),
    "FR": (
        (
            "Lucas", "Louis", "Gabriel", "Arthur", "Hugo", "Jules", "Leo", "Adam",
            "Raphael", "Paul", "Emma", "Jade", "Louise", "Alice", "Chloe", "Lina",
            "Mila", "Lea", "Manon", "Camille", "Ines", "Rose",
        ),
        (
            "Martin", "Bernard", "Thomas", "Petit", "Robert", "Richard", "Durand",
            "Dubois", "Moreau", "Laurent", "Simon", "Michel", "Lefebvre", "Leroy",
            "Roux", "David", "Bertrand", "Morel", "Fournier", "Girard", "Bonnet",
            "Dupont", "Lambert", "Fontaine",
        ),
    ),
    "NL": (
        (
            "Daan", "Sem", "Lucas", "Levi", "Finn", "Luuk", "Bram", "Noah", "Milan",
            "Jesse", "Emma", "Tess", "Sophie", "Julia", "Zoe", "Sara", "Anna", "Liv",
            "Nora", "Lotte", "Eva", "Fleur",
        ),
        (
            "de Jong", "Jansen", "de Vries", "van den Berg", "van Dijk", "Bakker",
            "Janssen", "Visser", "Smit", "Meijer", "de Boer", "Mulder", "de Groot",
            "Bos", "Vos", "Peters", "Hendriks", "van Leeuwen", "Dekker", "Brouwer",
        ),
    ),
    "IT": (
        (
            "Leonardo", "Francesco", "Alessandro", "Lorenzo", "Mattia", "Andrea",
            "Gabriele", "Tommaso", "Riccardo", "Edoardo", "Sofia", "Giulia", "Aurora",
            "Alice", "Ginevra", "Emma", "Giorgia", "Greta", "Beatrice", "Anna",
        ),
        (
            "Rossi", "Russo", "Ferrari", "Esposito", "Bianchi", "Romano", "Colombo",
            "Ricci", "Marino", "Greco", "Bruno", "Gallo", "Conti", "De Luca", "Mancini",
            "Costa", "Giordano", "Rizzo", "Lombardi", "Moretti",
        ),
    ),
    "ES": (
        (
            "Hugo", "Martin", "Lucas", "Mateo", "Leo", "Daniel", "Alejandro", "Pablo",
            "Manuel", "Alvaro", "Lucia", "Sofia", "Martina", "Maria", "Julia", "Paula",
            "Valeria", "Emma", "Daniela", "Carla",
        ),
        (
            "Garcia", "Rodriguez", "Gonzalez", "Fernandez", "Lopez", "Martinez",
            "Sanchez", "Perez", "Gomez", "Martin", "Jimenez", "Ruiz", "Hernandez",
            "Diaz", "Moreno", "Alvarez", "Munoz", "Romero", "Alonso", "Gutierrez",
        ),
    ),
    "PT": (
        (
            "Joao", "Francisco", "Santiago", "Afonso", "Duarte", "Tomas", "Martim",
            "Miguel", "Rodrigo", "Gabriel", "Maria", "Leonor", "Matilde", "Beatriz",
            "Carolina", "Alice", "Benedita", "Ines", "Lara", "Mariana",
        ),
        (
            "Silva", "Santos", "Ferreira", "Pereira", "Oliveira", "Costa", "Rodrigues",
            "Martins", "Jesus", "Sousa", "Fernandes", "Goncalves", "Gomes", "Lopes",
            "Marques", "Alves", "Almeida", "Ribeiro", "Pinto", "Carvalho",
        ),
    ),
    "BR": (
        (
            "Miguel", "Arthur", "Gael", "Heitor", "Theo", "Davi", "Gabriel", "Bernardo",
            "Samuel", "Joao", "Helena", "Alice", "Laura", "Maria", "Sophia", "Valentina",
            "Heloisa", "Julia", "Lorena", "Livia",
        ),
        (
            "Silva", "Santos", "Oliveira", "Souza", "Rodrigues", "Ferreira", "Alves",
            "Pereira", "Lima", "Gomes", "Costa", "Ribeiro", "Martins", "Carvalho",
            "Almeida", "Lopes", "Soares", "Fernandes", "Vieira", "Barbosa",
        ),
    ),
    "JP": (
        (
            "Haruto", "Yuto", "Sota", "Ren", "Hinata", "Asahi", "Minato", "Yamato",
            "Aoi", "Itsuki", "Yui", "Hina", "Yuna", "Sakura", "Mio", "Riko",
            "Mei", "Koharu", "Saki", "Nanami", "Ichika",
        ),
        (
            "Sato", "Suzuki", "Takahashi", "Tanaka", "Watanabe", "Ito", "Yamamoto",
            "Nakamura", "Kobayashi", "Kato", "Yoshida", "Yamada", "Sasaki", "Yamaguchi",
            "Matsumoto", "Inoue", "Kimura", "Hayashi", "Shimizu", "Yamazaki", "Mori",
            "Abe", "Ikeda", "Hashimoto",
        ),
    ),
    "KR": (
        (
            "Minjun", "Seojoon", "Dojun", "Siwoo", "Jihun", "Yejun", "Hajun", "Juwon",
            "Geonwoo", "Hyunwoo", "Seoah", "Jiwoo", "Haun", "Yuna", "Sua", "Jiyu",
            "Seoyeon", "Hayoon", "Chaeon", "Yeeun",
        ),
        (
            "Kim", "Lee", "Park", "Choi", "Jung", "Kang", "Cho", "Yoon", "Jang", "Lim",
            "Han", "Oh", "Shin", "Seo", "Kwon", "Hwang", "Ahn", "Song", "Hong", "Yoo",
        ),
    ),
    "CN": (
        (
            "Wei", "Fang", "Na", "Xiuying", "Min", "Jing", "Li", "Yang", "Yong", "Yan",
            "Lei", "Jun", "Qiang", "Jie", "Juan", "Tao", "Ming", "Chao", "Ping", "Hui",
        ),
        (
            "Wang", "Li", "Zhang", "Liu", "Chen", "Yang", "Huang", "Zhao", "Wu", "Zhou",
            "Xu", "Sun", "Ma", "Zhu", "Hu", "Guo", "He", "Gao", "Lin", "Luo",
        ),
    ),
    "TW": (
        (
            "Wei", "Ting", "Yu", "Jia", "An", "Cheng", "Hsuan", "Yi", "Chun", "Kai",
            "Yun", "Han", "Ming", "Chen", "Hao", "En", "Zih", "Rui", "Xin", "Pei",
        ),
        (
            "Chen", "Lin", "Huang", "Chang", "Lee", "Wang", "Wu", "Liu", "Tsai", "Yang",
            "Hsu", "Cheng", "Kuo", "Chiu", "Tseng", "Liao", "Hsieh", "Sung", "Tang",
        ),
    ),
    "HK": (
        (
            "Ka Ming", "Wai", "Chi", "Wing", "Ho", "Kin", "Yiu", "Man", "Siu", "Tsz",
            "Hiu", "Chun", "Lok", "Yin", "Shing", "Long", "Kei", "Yan", "Pui", "Mei",
        ),
        (
            "Chan", "Wong", "Cheung", "Lau", "Lee", "Ng", "Cheng", "Leung", "Ho", "Lam",
            "Yeung", "Tang", "Chow", "Choi", "Yuen", "Mak", "Kwok", "Fung", "Tsang", "Siu",
        ),
    ),
    "SG": _NAMES_EN,
    "MY": (
        (
            "Ahmad", "Muhammad", "Adam", "Daniel", "Ryan", "Aiman", "Hafiz", "Irfan",
            "Amir", "Azlan", "Siti", "Nur", "Aisha", "Aina", "Farah", "Hana", "Maya",
            "Sofia", "Izzati", "Putri",
        ),
        (
            "Abdullah", "Ahmad", "Ismail", "Hassan", "Ibrahim", "Yusof", "Rahman",
            "Chong", "Tan", "Lim", "Ng", "Lee", "Wong", "Goh", "Chan", "Ong", "Teo",
            "Kumar", "Singh", "Rajan",
        ),
    ),
    "PH": (
        (
            "Juan", "Jose", "Miguel", "Carlo", "Mark", "John", "Gabriel", "Angelo",
            "Paolo", "Rafael", "Maria", "Ana", "Sofia", "Andrea", "Angela", "Bianca",
            "Camille", "Denise", "Isabel", "Patricia",
        ),
        (
            "Santos", "Reyes", "Cruz", "Bautista", "Garcia", "Mendoza", "Torres",
            "Flores", "Gonzales", "Ramos", "Lopez", "Aquino", "Diaz", "Rivera",
            "Villanueva", "Castro", "Fernandez", "Navarro", "Domingo", "Santiago",
        ),
    ),
    "TH": (
        (
            "Nattapong", "Somchai", "Anan", "Somsak", "Wichai", "Pichai", "Kittisak",
            "Arthit", "Thanakorn", "Pattarapol", "Nattaya", "Siriporn", "Malee",
            "Kanokwan", "Supaporn", "Pornthip", "Apinya", "Chutima", "Wanida", "Rattana",
        ),
        (
            "Srisawat", "Chaiyaporn", "Saetang", "Wongchai", "Phanich", "Rattanakul",
            "Suksamran", "Boonsri", "Jaidee", "Thongchai", "Kittipong", "Prasert",
            "Siriwan", "Chanthara", "Phetchabun", "Wongsawat", "Anurak", "Kaewmanee",
        ),
    ),
    "VN": (
        (
            "Minh", "Anh", "Hung", "Dung", "Khoa", "Long", "Nam", "Phong", "Quang",
            "Tuan", "Linh", "Huong", "Trang", "Nga", "Thao", "Mai", "Lan", "Hoa",
            "Yen", "My",
        ),
        (
            "Nguyen", "Tran", "Le", "Pham", "Hoang", "Huynh", "Phan", "Vu", "Vo",
            "Dang", "Bui", "Do", "Ho", "Ngo", "Duong", "Ly", "Dinh", "Truong", "Doan",
            "Cao",
        ),
    ),
    "ID": (
        (
            "Budi", "Agus", "Andi", "Ahmad", "Rizky", "Dimas", "Fajar", "Hendra",
            "Yusuf", "Rian", "Siti", "Dewi", "Putri", "Ayu", "Rina", "Fitri", "Lestari",
            "Anisa", "Maya", "Intan",
        ),
        (
            "Santoso", "Wijaya", "Saputra", "Pratama", "Putra", "Hidayat", "Nugroho",
            "Setiawan", "Kurniawan", "Wibowo", "Siregar", "Nasution", "Halim", "Gunawan",
            "Tan", "Lim", "Suharto", "Rahman", "Maulana", "Firmansyah",
        ),
    ),
    "IN": (
        (
            "Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh", "Ayaan",
            "Krishna", "Ishaan", "Aadhya", "Ananya", "Aaradhya", "Pari", "Anika",
            "Navya", "Diya", "Myra", "Sara", "Ira",
        ),
        (
            "Sharma", "Verma", "Patel", "Singh", "Kumar", "Gupta", "Reddy", "Nair",
            "Iyer", "Chopra", "Mehta", "Joshi", "Malhotra", "Kapoor", "Rao", "Das",
            "Banerjee", "Choudhury", "Pandey", "Yadav",
        ),
    ),
    "TR": (
        (
            "Yusuf", "Eymen", "Mirac", "Omer", "Mustafa", "Kerem", "Emir", "Ahmet",
            "Ali", "Huseyin", "Zeynep", "Elif", "Asel", "Defne", "Azra", "Ecrin",
            "Miray", "Nehir", "Eylul", "Masal",
        ),
        (
            "Yilmaz", "Kaya", "Demir", "Celik", "Sahin", "Yildiz", "Yildirim", "Ozturk",
            "Aydin", "Ozdemir", "Arslan", "Dogan", "Kilic", "Aslan", "Cetin", "Kara",
            "Koc", "Kurt", "Ozkan", "Simsek",
        ),
    ),
    "AE": (
        (
            "Omar", "Youssef", "Khalid", "Hassan", "Ahmed", "Ali", "Mohammed", "Faisal",
            "Sultan", "Rashid", "Fatima", "Aisha", "Mariam", "Noor", "Layla", "Sara",
            "Huda", "Amira", "Salma", "Reem",
        ),
        (
            "Al Hashimi", "Al Maktoum", "Al Nahyan", "Al Qasimi", "Al Nuaimi", "Hassan",
            "Ahmed", "Ibrahim", "Ali", "Khan", "Rahman", "Hussein", "Abbas", "Farouk",
            "Salem", "Mansour", "Nasser", "Hamid", "Jaber", "Saeed",
        ),
    ),
    "SA": (
        (
            "Omar", "Youssef", "Khalid", "Hassan", "Ahmed", "Ali", "Mohammed", "Faisal",
            "Sultan", "Abdullah", "Fatima", "Aisha", "Mariam", "Noor", "Layla", "Sara",
            "Huda", "Noura", "Salma", "Reem",
        ),
        (
            "Al Saud", "Al Rashid", "Al Harbi", "Al Ghamdi", "Al Qahtani", "Al Otaibi",
            "Al Zahrani", "Al Shehri", "Al Dosari", "Al Mutairi", "Hassan", "Ahmed",
            "Ibrahim", "Ali", "Khan", "Rahman", "Hussein", "Abbas", "Salem", "Nasser",
        ),
    ),
    "MX": (
        (
            "Santiago", "Mateo", "Sebastian", "Leonardo", "Matias", "Emiliano", "Diego",
            "Daniel", "Miguel", "Angel", "Sofia", "Valentina", "Regina", "Camila",
            "Maria", "Ximena", "Victoria", "Renata", "Daniela", "Fernanda",
        ),
        (
            "Hernandez", "Garcia", "Martinez", "Lopez", "Gonzalez", "Perez", "Rodriguez",
            "Sanchez", "Ramirez", "Cruz", "Flores", "Gomez", "Morales", "Vazquez",
            "Jimenez", "Reyes", "Torres", "Diaz", "Gutierrez", "Ruiz",
        ),
    ),
    "AR": (
        (
            "Santiago", "Mateo", "Juan", "Benjamin", "Felipe", "Tomas", "Lucas", "Bruno",
            "Thiago", "Joaquin", "Sofia", "Valentina", "Emma", "Martina", "Catalina",
            "Olivia", "Isabella", "Mia", "Julieta",
        ),
        (
            "Gonzalez", "Rodriguez", "Gomez", "Fernandez", "Lopez", "Diaz", "Martinez",
            "Perez", "Garcia", "Sanchez", "Romero", "Sosa", "Alvarez", "Torres",
            "Ruiz", "Ramirez", "Flores", "Acosta", "Benitez", "Aguirre",
        ),
    ),
}

_NAMES_BY_REGION["UK"] = _NAMES_BY_REGION["GB"]
_NAMES_BY_REGION["IE"] = _NAMES_BY_REGION["GB"]
_NAMES_BY_REGION["CL"] = _NAMES_BY_REGION["AR"]
_NAMES_BY_REGION["CO"] = _NAMES_BY_REGION["MX"]


def random_person_name(region: str | None = None, language: str | None = None) -> tuple[str, str]:
    """Return (first_name, last_name) localized by region/language."""
    region_code = (region or "").strip().upper()
    lang = (language or "").strip()

    pool = _NAMES_BY_REGION.get(region_code)
    if not pool:
        low = lang.lower()
        if low.startswith("ja"):
            pool = _NAMES_BY_REGION["JP"]
        elif low.startswith("ko"):
            pool = _NAMES_BY_REGION["KR"]
        elif low.startswith("zh-tw"):
            pool = _NAMES_BY_REGION["TW"]
        elif low.startswith("zh-hk"):
            pool = _NAMES_BY_REGION["HK"]
        elif low.startswith("zh"):
            pool = _NAMES_BY_REGION["CN"]
        elif low.startswith("de"):
            pool = _NAMES_BY_REGION["DE"]
        elif low.startswith("fr"):
            pool = _NAMES_BY_REGION["FR"]
        elif low.startswith("tr"):
            pool = _NAMES_BY_REGION["TR"]
        elif low.startswith("es"):
            pool = _NAMES_BY_REGION["ES"]
        elif low.startswith("pt"):
            pool = _NAMES_BY_REGION["BR"]
        elif low.startswith("vi"):
            pool = _NAMES_BY_REGION["VN"]
        elif low.startswith("th"):
            pool = _NAMES_BY_REGION["TH"]
        elif low.startswith("id"):
            pool = _NAMES_BY_REGION["ID"]
        elif low.startswith("ar"):
            pool = _NAMES_BY_REGION["AE"]
        else:
            pool = _NAMES_EN

    first_pool, last_pool = pool
    return _rand_choice(first_pool), _rand_choice(last_pool)


def random_profile(
    *,
    region: str | None = None,
    browser: str | None = None,
    platform_os: str | None = None,
    chrome_major: int | None = None,
    hardware_profile: str | None = None,
) -> Profile:
    """Generate a randomized browser fingerprint.

    Args:
        region: ISO country/region code (JP/US/TR...). Affects locale + timezone.
        browser: "chrome" | "edge" | None(default Chrome)
        platform_os: "Windows" | "macOS" | None(default Windows)
    """
    region_code = (region or "").strip().upper()
    requested_major = int(chrome_major or 0)
    major = (
        requested_major
        if 100 <= requested_major <= 999
        else _rand_choice(_REALISTIC_MAJORS)
    )

    # The bundled transport preset is desktop Chrome/Windows.  Explicit
    # overrides remain available for non-registration callers, but automatic
    # generation never mixes a macOS/Edge JS profile into Chrome TLS.
    if platform_os in {"Windows", "macOS"}:
        os_name = platform_os
    else:
        os_name = "Windows"

    if browser in {"chrome", "edge"}:
        browser_name = browser
    else:
        browser_name = "chrome"

    user_agent, sec_ch_ua, ch_platform, full_ver, browser_name = _build_ua_and_ch(
        major, browser_name, os_name
    )
    locale, language = _pick_locale(region_code)
    tz_offset, tz_label, tz_id = _pick_timezone(region_code)
    hardware_name, hardware = _pick_hardware(os_name, hardware_profile)
    (
        resolution,
        cores,
        device_memory,
        max_touch_points,
        platform_version,
        architecture,
        webgl_vendor,
        webgl_renderer,
    ) = _hardware_values(hardware)

    return Profile(
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        sec_ch_ua_platform=ch_platform,
        locale=locale,
        language=language,
        resolution=resolution,
        cores=cores,
        history_len=_rand_choice(_HISTORY_LENGTHS),
        timezone_offset_min=tz_offset,
        timezone_label=tz_label,
        platform_os=os_name,
        browser=browser_name,
        chrome_major=major,
        chrome_full_version=full_ver,
        device_memory=device_memory,
        max_touch_points=max_touch_points,
        region=region_code,
        timezone_id=tz_id,
        tls_impersonate=choose_tls_impersonate(
            browser=browser_name,
            chrome_major=major,
            platform_os=os_name,
        ),
        tls_client_identifier=choose_tls_client_identifier(
            browser=browser_name,
            browser_major=major,
        ),
        platform_version=platform_version,
        architecture=architecture,
        hardware_profile=hardware_name,
        webgl_vendor=webgl_vendor,
        webgl_renderer=webgl_renderer,
    )


def apply_region_fingerprint(profile: Profile, region: str | None) -> Profile:
    """Align locale/timezone to region while keeping UA/device stable within task."""
    region_code = (region or "").strip().upper()
    if not region_code:
        return profile
    # Idempotence matters: pipeline retries may call this helper several times
    # for the same provider region.  Re-rolling locale/timezone under the same
    # device id is a fingerprint drift.
    if profile.region == region_code and profile.locale and profile.timezone_id:
        return profile
    locale, language = _pick_locale(region_code)
    tz_offset, tz_label, tz_id = _pick_timezone(region_code)
    profile.locale = locale
    profile.language = language
    profile.timezone_offset_min = tz_offset
    profile.timezone_label = tz_label
    profile.timezone_id = tz_id
    profile.region = region_code
    return profile


def apply_proxy_timezone(
    profile: Profile,
    proxy: str | None,
    *,
    geo_country: str | None = None,
    geo_timezone: str | None = None,
) -> Profile:
    """Mutate profile locale/timezone from proxy region or geo country.

    Priority:
      1) observed exit-IP country/timezone
      2) provider region in proxy URL (region-JP / country-US)
    """
    region = (geo_country or "").strip().upper() or proxy_region_code(proxy)
    if not region:
        return profile
    apply_region_fingerprint(profile, region)
    timezone_id = str(geo_timezone or "").strip()
    if timezone_id and profile.timezone_id != timezone_id:
        offset, label, timezone_id = _timezone_parts(timezone_id, region)
        profile.timezone_offset_min = offset
        profile.timezone_label = label
        profile.timezone_id = timezone_id
    return profile


def edge_windows_profile(major: int = 146, *, region: str | None = None) -> Profile:
    """Profile matching local successful Edge/Windows packet capture style."""
    region_code = (region or "").strip().upper()
    locale, language = _pick_locale(region_code or "US")
    tz_offset, tz_label, tz_id = _pick_timezone(region_code or "JP")
    full = _chrome_full_version(major)
    hardware_name, hardware = _pick_hardware("Windows")
    (
        resolution,
        cores,
        device_memory,
        max_touch_points,
        platform_version,
        architecture,
        webgl_vendor,
        webgl_renderer,
    ) = _hardware_values(hardware)
    return Profile(
        user_agent=(
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36 Edg/{major}.0.0.0"
        ),
        sec_ch_ua=(
            f'"Not;A=Brand";v="8", "Chromium";v="{major}", '
            f'"Microsoft Edge";v="{major}"'
        ),
        sec_ch_ua_platform='"Windows"',
        locale=locale,
        language=language,
        resolution=resolution,
        cores=cores,
        history_len=_rand_choice(_HISTORY_LENGTHS),
        timezone_offset_min=tz_offset,
        timezone_label=tz_label,
        platform_os="Windows",
        browser="edge",
        chrome_major=major,
        chrome_full_version=full,
        device_memory=device_memory,
        max_touch_points=max_touch_points,
        region=region_code,
        timezone_id=tz_id,
        tls_impersonate=choose_tls_impersonate(
            browser="edge",
            chrome_major=major,
            platform_os="Windows",
        ),
        tls_client_identifier=choose_tls_client_identifier(
            browser="edge",
            browser_major=major,
        ),
        platform_version=platform_version,
        architecture=architecture,
        hardware_profile=hardware_name,
        webgl_vendor=webgl_vendor,
        webgl_renderer=webgl_renderer,
    )


def chrome_windows_profile(major: int | None = None, *, region: str | None = None) -> Profile:
    """Chrome/Windows profile using one exact bundled TLS preset per account."""
    return random_profile(
        region=region,
        browser="chrome",
        platform_os="Windows",
        chrome_major=major,
    )


def profile_from_user_agent(
    user_agent: str,
    *,
    region: str | None = None,
    hardware_profile: str | None = None,
    audio_seed: str | None = None,
) -> Profile:
    """Create a complete runtime profile from an observed browser UA."""
    ua = str(user_agent or "").strip()
    # Chromium's headless launcher may expose this token even when every other
    # runtime field is browser-shaped.  A context UA must use the regular token.
    ua = re.sub(r"\bHeadlessChrome/", "Chrome/", ua, flags=re.I)
    firefox = re.search(r"Firefox/(\d+)(?:\.([\d.]+))?", ua, flags=re.I)
    if firefox:
        profile = firefox_windows_profile(
            int(firefox.group(1)),
            region=region,
            hardware_profile=hardware_profile,
            audio_seed=audio_seed,
        )
        profile.user_agent = ua
        full_match = re.search(r"Firefox/([\d.]+)", ua, flags=re.I)
        if full_match:
            profile.chrome_full_version = full_match.group(1)
        seed = profile.audio_seed or secrets.token_hex(4).upper()
        profile.device_name, profile.mac_address = _device_identity(
            profile.platform_os, seed
        )
        return profile

    chrome = re.search(r"(?:Chrome|Chromium)/(\d+)(?:\.([\d.]+))?", ua, flags=re.I)
    edge = re.search(r"Edg/(\d+)(?:\.([\d.]+))?", ua, flags=re.I)
    if chrome:
        major = int(chrome.group(1))
        platform_os = "macOS" if "Macintosh" in ua else "Windows"
        browser = "edge" if edge else "chrome"
        profile = random_profile(
            region=region,
            browser=browser,
            platform_os=platform_os,
            chrome_major=major,
            hardware_profile=hardware_profile,
        )
        profile.user_agent = ua
        full_match = re.search(r"(?:Chrome|Chromium)/([\d.]+)", ua, flags=re.I)
        if full_match:
            profile.chrome_full_version = full_match.group(1)
        seed = (
            str(audio_seed or "").strip().upper() or secrets.token_hex(4).upper()
        )
        profile.audio_seed = seed
        profile.audio_mode = "noise"
        profile.media_devices_mode = "noise"
        profile.media_devices_seed = _derived_seed(seed, "media")
        profile.client_rects_mode = "noise"
        profile.client_rects_seed = _derived_seed(seed, "rects")
        profile.speech_voices_mode = "noise"
        profile.speech_voices_seed = _derived_seed(seed, "voices")
        profile.webgpu_mode = "webgl"
        profile.port_scan_protection = True
        profile.device_name, profile.mac_address = _device_identity(
            profile.platform_os, seed
        )
        return profile
    raise ValueError("unsupported browser user agent")


def firefox_windows_profile(
    major: int = 150,
    *,
    region: str | None = None,
    hardware_profile: str | None = None,
    audio_seed: str | None = None,
) -> Profile:
    """Build a Firefox/Windows profile without Chromium-only Client Hints.

    The default ``win_gtx1660`` persona mirrors the supplied 20-core/8-GB/GTX
    1660-style browser profile.  Device name and MAC address are intentionally
    absent: ordinary web content cannot read either value.
    """
    major = max(100, int(major or 150))
    region_code = (region or "").strip().upper()
    locale, language = _pick_locale(region_code)
    tz_offset, tz_label, tz_id = _pick_timezone(region_code)
    hardware_name, hardware = _pick_hardware(
        "Windows", hardware_profile or "win_gtx1660"
    )
    (
        resolution,
        cores,
        device_memory,
        max_touch_points,
        platform_version,
        architecture,
        webgl_vendor,
        webgl_renderer,
    ) = _hardware_values(hardware)
    seed = str(audio_seed or "").strip().upper()
    if not seed:
        seed = secrets.token_hex(4).upper()
    device_name, mac_address = _device_identity("Windows", seed)
    full = f"{major}.0"
    return Profile(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; "
            f"rv:{full}) Gecko/20100101 Firefox/{full}"
        ),
        sec_ch_ua="",
        sec_ch_ua_platform="",
        locale=locale,
        language=language,
        resolution=resolution,
        cores=cores,
        history_len=_rand_choice(_HISTORY_LENGTHS),
        timezone_offset_min=tz_offset,
        timezone_label=tz_label,
        platform_os="Windows",
        browser="firefox",
        chrome_major=major,
        chrome_full_version=full,
        device_memory=device_memory,
        max_touch_points=max_touch_points,
        region=region_code,
        timezone_id=tz_id,
        tls_impersonate=choose_tls_impersonate(
            browser="firefox",
            chrome_major=major,
            platform_os="Windows",
        ),
        tls_client_identifier=choose_tls_client_identifier(
            browser="firefox", browser_major=major
        ),
        platform_version=platform_version,
        architecture=architecture,
        hardware_profile=hardware_name,
        webgl_vendor=webgl_vendor,
        webgl_renderer=webgl_renderer,
        webrtc_mode="disabled",
        geolocation_mode="ask",
        canvas_mode="real",
        webgl_mode="real",
        audio_mode="noise",
        audio_seed=seed,
        device_name=device_name,
        mac_address=mac_address,
        hardware_acceleration="default",
    )


def _full_version_list(profile: Profile) -> str:
    major = str(profile.browser_major)
    full = profile.browser_full_version
    return re.sub(
        rf'v="{re.escape(major)}"',
        f'v="{full}"',
        profile.sec_ch_ua,
    )


def profile_headers(profile: Profile, *, high_entropy: bool = False) -> dict[str, str]:
    """Build UA headers while omitting browser-incompatible fields."""
    headers = {
        "user-agent": profile.user_agent,
        "accept-language": profile.locale,
    }
    if profile.sec_ch_ua:
        headers.update(
            {
                "sec-ch-ua": profile.sec_ch_ua,
                "sec-ch-ua-mobile": "?1" if profile.mobile else "?0",
                "sec-ch-ua-platform": profile.sec_ch_ua_platform,
            }
        )
        if high_entropy:
            headers.update(
                {
                    "sec-ch-ua-arch": f'"{profile.architecture}"',
                    "sec-ch-ua-bitness": f'"{profile.bitness}"',
                    "sec-ch-ua-model": '""',
                    "sec-ch-ua-full-version": f'"{profile.browser_full_version}"',
                    "sec-ch-ua-full-version-list": _full_version_list(profile),
                    "sec-ch-ua-platform-version": f'"{profile.platform_version}"',
                }
            )
    return headers


@dataclass(frozen=True, slots=True)
class ProfileValidation:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors


def validate_profile(profile: Profile) -> ProfileValidation:
    """Detect UA/Client-Hint/TLS/hardware contradictions before launch."""
    errors: list[str] = []
    warnings: list[str] = []
    browser = profile.browser.lower()
    major = profile.browser_major

    if browser == "firefox":
        if f"Firefox/{major}." not in profile.user_agent:
            errors.append("Firefox UA major does not match browser_major")
        if profile.sec_ch_ua or profile.sec_ch_ua_platform:
            errors.append("Firefox profile must not emit Chromium sec-ch-ua headers")
        if not profile.tls_impersonate.startswith("firefox"):
            errors.append("Firefox UA is paired with a non-Firefox curl TLS preset")
        if not profile.tls_client_identifier.startswith("firefox_"):
            errors.append("Firefox UA is paired with a non-Firefox tls-client preset")
    elif browser in {"chrome", "edge"}:
        if f"Chrome/{major}." not in profile.user_agent:
            errors.append("Chromium UA major does not match browser_major")
        if f'"Chromium";v="{major}"' not in profile.sec_ch_ua:
            errors.append("Chromium sec-ch-ua major does not match browser_major")
        expected_platform = f'"{profile.platform_os}"'
        if profile.sec_ch_ua_platform != expected_platform:
            errors.append("sec-ch-ua-platform does not match platform_os")
        if not profile.browser_full_version.startswith(f"{major}."):
            errors.append("Chromium full version does not match browser_major")

    if not re.fullmatch(r"[1-9]\d{2,4}x[1-9]\d{2,4}", profile.resolution):
        errors.append("resolution must be WIDTHxHEIGHT")
    if profile.cores <= 0 or profile.device_memory <= 0:
        errors.append("hardware concurrency and memory must be positive")
    persona = _HARDWARE_PERSONAS.get(profile.hardware_profile)
    if persona is None:
        errors.append("unknown hardware persona")
    elif persona["platform"] != profile.platform_os:
        errors.append("hardware persona platform does not match UA platform")
    if persona:
        if profile.resolution not in persona["resolutions"]:
            errors.append("resolution does not belong to hardware persona")
        if profile.cores not in persona["cores"]:
            errors.append("core count does not belong to hardware persona")
        if profile.device_memory not in persona["memory"]:
            errors.append("memory does not belong to hardware persona")
        if profile.max_touch_points not in persona["touch"]:
            errors.append("touch points do not belong to hardware persona")
        if profile.platform_version not in persona["platform_versions"]:
            errors.append("platform version does not belong to hardware persona")
        if profile.architecture != persona["architecture"]:
            errors.append("architecture does not belong to hardware persona")
        if (profile.webgl_vendor, profile.webgl_renderer) not in persona["webgl"]:
            errors.append("WebGL renderer does not belong to hardware persona")

    region = profile.region.upper()
    allowed_locales = _LOCALES_BY_REGION.get(region)
    if allowed_locales and (profile.locale, profile.language) not in allowed_locales:
        errors.append("locale/language pair does not match region")
    allowed_timezones = _IANA_BY_REGION.get(region)
    if allowed_timezones and profile.timezone_id not in allowed_timezones:
        errors.append("timezone does not match region")
    expected_tls = choose_tls_impersonate(
        browser=profile.browser,
        chrome_major=profile.browser_major,
        platform_os=profile.platform_os,
    )
    if profile.tls_impersonate != expected_tls:
        errors.append("curl TLS preset does not match the supported browser mapping")

    tls_match = re.search(r"(\d+)", profile.tls_impersonate)
    if tls_match and abs(int(tls_match.group(1)) - major) > 0:
        warnings.append(
            f"curl TLS preset {profile.tls_impersonate} is a nearest-version fallback "
            f"for {browser}/{major}"
        )
    return ProfileValidation(tuple(errors), tuple(warnings))


FINGERPRINT_PROFILE_SCHEMA_VERSION = 2


def profile_to_json(profile: Profile) -> str:
    """Serialize a complete, versioned account fingerprint snapshot."""
    payload = {
        "version": FINGERPRINT_PROFILE_SCHEMA_VERSION,
        "profile": asdict(profile),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def profile_from_json(value: object) -> Profile | None:
    """Restore and validate a stored fingerprint snapshot.

    Unknown future fields are ignored so additive schema changes remain
    backward compatible. Invalid or truncated snapshots raise ``ValueError``
    and callers can generate a new legacy-account companion profile.
    """
    if value in (None, "", b""):
        return None
    try:
        payload = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid fingerprint profile JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("fingerprint profile snapshot must be an object")
    raw = payload.get("profile", payload)
    if not isinstance(raw, dict):
        raise ValueError("fingerprint profile payload must be an object")
    allowed = {item.name for item in fields(Profile)}
    kwargs = {key: raw[key] for key in allowed if key in raw}
    try:
        profile = Profile(**kwargs)
    except (TypeError, ValueError) as exc:
        raise ValueError("fingerprint profile fields are incomplete") from exc
    validation = validate_profile(profile)
    if not validation.valid:
        raise ValueError(
            "stored fingerprint profile mismatch: " + "; ".join(validation.errors)
        )
    return profile


def profile_supports_protocol_transport(profile: Profile) -> bool:
    """Return whether the stored UA has an exact installed TLS persona."""
    identifier = str(profile.tls_client_identifier or "").lower()
    browser = str(profile.browser or "").lower()
    if browser == "chrome":
        expected = "chrome_"
    elif browser == "firefox":
        expected = "firefox_"
    else:
        return False
    match = re.search(r"(\d+)", identifier)
    return bool(
        identifier.startswith(expected)
        and match
        and int(match.group(1)) == int(profile.browser_major)
    )


def profile_summary(profile: Profile) -> str:
    """Short one-line fingerprint for logs."""
    tls = (
        getattr(profile, "tls_client_identifier", "")
        or getattr(profile, "tls_impersonate", "")
        or ""
    )
    return (
        f"{profile.browser}/{profile.chrome_full_version} {profile.platform_os} · "
        f"{profile.language} · tz={profile.timezone_offset_min}({profile.timezone_label}) · "
        f"{profile.resolution} · {profile.cores}C/{profile.device_memory:g}GB · "
        f"hist={profile.history_len} · touch={profile.max_touch_points} · "
        f"hw={profile.hardware_profile}"
        + (f" · tls={tls}" if tls else "")
        + (f" · region={profile.region}" if profile.region else "")
    )
