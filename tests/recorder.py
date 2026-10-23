"""Step recorder: captioned frames of the plugin UI, joined into an MP4.

Works with the offscreen Qt platform: every frame is rendered from the
widgets themselves (``QWidget.grab``), so no display server is needed.
A frame shows the main QGIS window with every visible dialog composited
on top, a header with the test name and a caption bar for the step.
"""

import pathlib
import shutil
import subprocess

from qgis.PyQt.QtCore import QPoint, QRect, Qt
from qgis.PyQt.QtGui import QColor, QFont, QImage, QPainter, QPen
from qgis.PyQt.QtWidgets import QApplication, QDialog, QMainWindow, QMessageBox

FRAME_W, FRAME_H = 1280, 860
HEADER_H, CAPTION_H = 44, 84
STAGE_H = FRAME_H - HEADER_H - CAPTION_H

INK = QColor("#1b2230")
PAPER = QColor("#f6f7f9")
MUTED = QColor("#5b6577")
BUG = QColor("#c0392b")
OK = QColor("#23824a")


class Recorder:
    def __init__(self, title, main_window=None):
        self.title = title
        self.main = main_window
        self.frames = []          # [(QImage, seconds)]
        self.captions = []        # [(caption, kind)] parallel to frames

    # ------------------------------------------------------------------
    # capture
    # ------------------------------------------------------------------

    def step(self, caption, seconds=1.8, kind="step", widget=None):
        """Capture the current UI with *caption*.

        *kind* is ``step``, ``bug`` (red banner, held longer) or ``ok``.
        *widget* forces a specific widget to the front of the frame.
        """
        QApplication.processEvents()
        stage = self._stage(widget)
        frame = self._decorate(stage, caption, kind)
        if kind == "bug":
            seconds = max(seconds, 4.0)
        self.frames.append((frame, seconds))
        self.captions.append((caption, kind))

    def show_message_box(self, kind, title, text, caption=None, frame_kind="step"):
        """Render the QMessageBox the plugin asked for, then close it."""
        icons = {
            "critical": QMessageBox.Icon.Critical,
            "warning": QMessageBox.Icon.Warning,
            "question": QMessageBox.Icon.Question,
            "information": QMessageBox.Icon.Information,
        }
        box = QMessageBox(icons.get(kind, QMessageBox.Icon.NoIcon), title, text,
                          parent=self.main)
        if kind in ("question", "warning"):
            box.setStandardButtons(QMessageBox.StandardButton.Yes
                                   | QMessageBox.StandardButton.No)
        box.show()
        QApplication.processEvents()
        self.step(caption or f"QGIS shows: “{title}”", seconds=2.6,
                  kind=frame_kind, widget=box)
        box.close()
        box.deleteLater()

    def _visible_dialogs(self):
        out = []
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                out.append(w)
        return out

    def _stage(self, front=None):
        stage = QImage(FRAME_W, STAGE_H, QImage.Format.Format_RGB32)
        stage.fill(QColor("#d9dde4"))
        p = QPainter(stage)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self.main is not None and self.main.isVisible():
            img = self.main.grab().toImage()
            self._draw_fit(p, img, QRect(0, 0, FRAME_W, STAGE_H))
        dialogs = [d for d in self._visible_dialogs() if d is not front]
        if front is not None:
            dialogs.append(front)
        for i, dlg in enumerate(dialogs):
            img = dlg.grab().toImage()
            # cascade dialogs, newest in front, centred on the stage
            box = QRect(0, 0, min(img.width(), FRAME_W - 80),
                        min(img.height(), STAGE_H - 40))
            box.moveCenter(QPoint(FRAME_W // 2 + i * 24, STAGE_H // 2 + i * 18))
            shadow = box.adjusted(6, 8, 6, 8)
            p.fillRect(shadow, QColor(0, 0, 0, 70))
            self._draw_fit(p, img, box)
            p.setPen(QPen(QColor("#8a93a3"), 1))
            p.drawRect(box)
        p.end()
        return stage

    @staticmethod
    def _draw_fit(p, img, rect):
        scaled = img.scaled(rect.width(), rect.height(),
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        x = rect.x() + (rect.width() - scaled.width()) // 2
        y = rect.y() + (rect.height() - scaled.height()) // 2
        p.drawImage(x, y, scaled)

    def _decorate(self, stage, caption, kind):
        frame = QImage(FRAME_W, FRAME_H, QImage.Format.Format_RGB32)
        frame.fill(PAPER)
        p = QPainter(frame)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        # header: test name + step counter
        p.fillRect(0, 0, FRAME_W, HEADER_H, INK)
        f = QFont("DejaVu Sans", 13)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#ffffff"))
        p.drawText(QRect(16, 0, FRAME_W - 200, HEADER_H),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self.title)
        f2 = QFont("DejaVu Sans Mono", 11)
        p.setFont(f2)
        p.setPen(QColor("#aab4c3"))
        p.drawText(QRect(FRAME_W - 200, 0, 184, HEADER_H),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                   f"step {len(self.frames) + 1}")

        p.drawImage(0, HEADER_H, stage)

        # caption bar
        y = HEADER_H + STAGE_H
        colour = {"bug": BUG, "ok": OK}.get(kind, QColor("#ffffff"))
        p.fillRect(0, y, FRAME_W, CAPTION_H,
                   colour if kind != "step" else QColor("#ffffff"))
        p.fillRect(0, y, FRAME_W, 1, QColor("#c9ced8"))
        text_colour = QColor("#ffffff") if kind != "step" else INK
        left = 20
        if kind in ("bug", "ok"):
            badge = "BUG" if kind == "bug" else "OK"
            fb = QFont("DejaVu Sans Mono", 12)
            fb.setBold(True)
            p.setFont(fb)
            p.setPen(QPen(QColor("#ffffff"), 2))
            r = QRect(20, y + CAPTION_H // 2 - 15, 64, 30)
            p.drawRect(r)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, badge)
            left = 100
        fc = QFont("DejaVu Sans", 14)
        p.setFont(fc)
        p.setPen(text_colour)
        p.drawText(QRect(left, y + 6, FRAME_W - left - 20, CAPTION_H - 12),
                   Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap,
                   caption)
        p.end()
        return frame

    # ------------------------------------------------------------------
    # output
    # ------------------------------------------------------------------

    def _write_text_tracks(self, out_dir, name):
        """``<name>.srt`` subtitles and ``<name>.steps.txt`` action log."""
        def ts(t):
            ms = int(round(t * 1000))
            h, ms = divmod(ms, 3600000)
            m, ms = divmod(ms, 60000)
            s, ms = divmod(ms, 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        srt, steps, t = [], [f"{self.title}", ""], 0.0
        for i, ((_, secs), (caption, kind)) in enumerate(zip(self.frames, self.captions), 1):
            tag = {"bug": "BUG ", "ok": "OK  "}.get(kind, "")
            srt += [str(i), f"{ts(t)} --> {ts(t + secs)}", f"{tag}{caption}", ""]
            steps.append(f"{i:>2}. [{ts(t)[3:8]}] {tag}{caption}")
            t += secs
        srt_path = out_dir / f"{name}.srt"
        srt_path.write_text("\n".join(srt), encoding="utf-8")
        (out_dir / f"{name}.steps.txt").write_text("\n".join(steps) + "\n",
                                                   encoding="utf-8")
        return srt_path

    def save(self, out_dir, name):
        """Write ``<name>.mp4`` (with a subtitle track), ``<name>.png``
        (poster = last frame), ``<name>.srt`` and ``<name>.steps.txt``."""
        if not self.frames:
            return None
        out_dir = pathlib.Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        srt_path = self._write_text_tracks(out_dir, name)
        work = out_dir / f".{name}.frames"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        lines = []
        for i, (img, secs) in enumerate(self.frames):
            path = work / f"{i:03d}.png"
            img.save(str(path))
            lines += [f"file '{path.name}'", f"duration {secs}"]
        lines.append(f"file '{len(self.frames) - 1:03d}.png'")  # concat quirk
        (work / "list.txt").write_text("\n".join(lines) + "\n")
        poster = out_dir / f"{name}.png"
        self.frames[-1][0].save(str(poster))
        video = out_dir / f"{name}.mp4"
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                 "-i", "list.txt", "-i", str(srt_path.resolve()),
                 "-map", "0:v", "-map", "1:s",
                 "-vf", "fps=25,format=yuv420p",
                 "-c:v", "libx264", "-preset", "medium", "-crf", "23",
                 "-c:s", "mov_text", "-metadata:s:s:0", "language=eng",
                 "-movflags", "+faststart", str(video.resolve())],
                cwd=work, check=True,
            )
        shutil.rmtree(work, ignore_errors=True)
        return video if video.exists() else poster
