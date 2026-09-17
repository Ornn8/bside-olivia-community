from __future__ import annotations

import cv2
import numpy as np

from runtime.personal_chat.qr_svg import matrix


def test_personal_chat_qr_round_trips_through_opencv() -> None:
    value = "https://liteapp.weixin.qq.com/q/synthetic-login"
    modules = matrix(value)
    border = 4
    scale = 8
    side = (len(modules) + border * 2) * scale
    image = np.full((side, side), 255, dtype=np.uint8)
    for y, row in enumerate(modules):
        for x, dark in enumerate(row):
            if not dark:
                continue
            top = (y + border) * scale
            left = (x + border) * scale
            image[top : top + scale, left : left + scale] = 0

    decoded, points, _straight = cv2.QRCodeDetector().detectAndDecode(image)
    assert points is not None
    assert decoded == value
