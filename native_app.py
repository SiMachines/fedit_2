import dearpygui.dearpygui as dpg
import sys
import os
import time
import webbrowser
from collections import deque
from dataclasses import dataclass, field
import uuid
import json
import math
import bisect
from typing import Optional
import warnings
import ctypes
import threading

# Filter benign PySDL2 warning
warnings.filterwarnings("ignore", message="pysdl2-dll is installed as source-only")
import ctypes

# Fix for finding modules in .dependencies when running locally or frozen
if getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
    os.environ["PYSDL2_DLL_PATH"] = base_path
else:
    base_path = os.path.dirname(os.path.abspath(__file__))
    sys.path.append(os.path.join(base_path, '.dependencies'))
from server.ffb_engine import engine, DeviceInfo
from server.serial_manager import serial_manager, scan_ports
from server.sweep_engine import sweep_engine, SweepConfig, SweepData
 
# --- Data Model ---
@dataclass
class Clip:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    type: str = "Sine"
    start_time: float = 0.0
    duration: float = 2.0
    track_index: int = 0
    magnitude: int = 3276
    frequency: int = 10
    frequency_end: int = 10
    start_phase: int = 0
    lock_phase: bool = False
    sweep_enabled: bool = False
    direction_mode: str = "Polar"
    angle: int = 90
    radius: int = 1
    x: int = 1
    y: int = 0
    z: int = 0
    yaw: int = 0
    pitch: int = 0
    distance: int = 1
    start_mag: int = -10000
    end_mag: int = 10000
    attack_length: int = 0
    fade_length: int = 0
    name: str = "Clip"
    active_effect_id: int = -1

    def to_dict(self):
        return {
            "id": self.id,
            "type": self.type,
            "start_time": self.start_time,
            "duration": self.duration,
            "track_index": self.track_index,
            "magnitude": self.magnitude,
            "frequency": self.frequency,
            "frequency_end": self.frequency_end,
            "sweep_enabled": self.sweep_enabled,
            "start_phase": self.start_phase,
            "lock_phase": self.lock_phase,
            "direction_mode": self.direction_mode,
            "angle": self.angle,
            "radius": self.radius,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "yaw": self.yaw,
            "pitch": self.pitch,
            "distance": self.distance,
            "start_mag": self.start_mag,
            "end_mag": self.end_mag,
            "attack_length": self.attack_length,
            "fade_length": self.fade_length,
            "name": self.name,
        }

    @staticmethod
    def from_dict(d):
        freq = d.get("frequency", 10)
        raw_angle = d.get("angle", 90)
        # Backward compatibility: older projects stored hundredths of a degree
        if raw_angle > 359:
            raw_angle = int(round(raw_angle / 100.0))
        raw_angle = max(0, min(359, raw_angle))

        return Clip(
            id=d.get("id", str(uuid.uuid4())),
            type=d.get("type", "Sine"),
            start_time=d.get("start_time", 0.0),
            duration=d.get("duration", 1.0),
            track_index=d.get("track_index", 0),
            magnitude=d.get("magnitude", 3276),
            frequency=freq,
            frequency_end=d.get("frequency_end", freq),
            start_phase=d.get("start_phase", 0),
            lock_phase=d.get("lock_phase", False),
            sweep_enabled=d.get("sweep_enabled", False),
            direction_mode=d.get("direction_mode", "Polar"),
            angle=raw_angle,
            radius=d.get("radius", 1),
            x=d.get("x", 1),
            y=d.get("y", 0),
            z=d.get("z", 0),
            yaw=d.get("yaw", 0),
            pitch=d.get("pitch", 0),
            distance=d.get("distance", 1),
            start_mag=d.get("start_mag", -10000),
            end_mag=d.get("end_mag", 10000),
            attack_length=d.get("attack_length", 0),
            fade_length=d.get("fade_length", 0),
            name=d.get("name", "Clip"),
        )
 
@dataclass
class Track:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "Track"
    gain: int = 100
    clips: list[Clip] = field(default_factory=list)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "gain": self.gain,
            "clips": [c.to_dict() for c in self.clips],
        }

    @staticmethod
    def from_dict(d):
        track = Track(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "Track"),
            gain=d.get("gain", 100),
        )
        track.clips = [Clip.from_dict(cd) for cd in d.get("clips", [])]
        return track


class MotorBusterSequencer:
    def __init__(self):
        self.tracks = [Track(name=f"{i+1}") for i in range(4)]
        self.is_playing = False
        self.current_time = 0.0
        self.last_tick = 0.0
        self.zoom_x = 50.0
        self.selected_clip: Clip = None
        self.drag_clip: Clip = None
        self.drag_time_offset: float = 0.0
        self.resize_clip: Clip = None
        self.resize_edge: str = None
        self.resize_initial_time: float = 0.0
        self.resize_initial_dur: float = 0.0
        self.clipboard: dict = None

    def to_dict(self):
        return {"tracks": [t.to_dict() for t in self.tracks]}

    def load_from_dict(self, d):
        self.tracks = [Track.from_dict(td) for td in d.get("tracks", [])]
        self.selected_clip = None

    def add_clip(self, track_idx, type, start_time):
        clip = Clip(type=type, start_time=start_time, track_index=track_idx)
        self.tracks[track_idx].clips.append(clip)
        return clip

    def delete_clip(self, clip):
        for t in self.tracks:
            if clip in t.clips:
                t.clips.remove(clip)
                if self.selected_clip == clip:
                    self.selected_clip = None
                return

    def get_clip_at(self, track_idx, time_s):
        for clip in self.tracks[track_idx].clips:
            if clip.start_time <= time_s <= clip.start_time + clip.duration:
                return clip
        return None

    def get_clip_at_precise(self, track_idx, time_s):
        for clip in self.tracks[track_idx].clips:
            if clip.start_time <= time_s < clip.start_time + clip.duration:
                return clip
        return None

    def get_clip_by_id(self, clip_id):
        if clip_id is None:
            return None
        for track in self.tracks:
            for clip in track.clips:
                if clip.id == clip_id:
                    return clip
        return None


class InspectorPanel:
    def __init__(self, app, parent, clip=None, pos=[400, 200]):
        self.app = app
        self.clip = clip # If None, acts as "Live" inspector for selection
        self.parent = parent
        self.id = str(uuid.uuid4())[:8]
        
        # Unique Tags
        self.tag_tab = f"tab_{self.id}"
        self.tag_start = f"insp_start_{self.id}"
        self.tag_dur = f"insp_dur_{self.id}"
        self.tag_mag = f"insp_mag_{self.id}"
        self.tag_freq = f"insp_freq_{self.id}"
        self.tag_freq_end = f"insp_freq_end_{self.id}"
        self.tag_phase = f"insp_phase_{self.id}"
        self.tag_lock_phase = f"insp_lockphase_{self.id}"
        self.tag_sweep = f"insp_sweep_{self.id}"
        self.tag_title = f"insp_title_{self.id}"
        self.tag_type = f"insp_type_{self.id}"
        self.tag_dir_mode = f"insp_dir_mode_{self.id}"
        self.tag_angle = f"insp_angle_{self.id}"
        self.tag_radius = f"insp_radius_{self.id}"
        self.tag_x = f"insp_x_{self.id}"
        self.tag_y = f"insp_y_{self.id}"
        self.tag_z = f"insp_z_{self.id}"
        self.tag_yaw = f"insp_yaw_{self.id}"
        self.tag_pitch = f"insp_pitch_{self.id}"
        self.tag_distance = f"insp_distance_{self.id}"
        self.tag_start_mag = f"insp_startmag_{self.id}"
        self.tag_end_mag = f"insp_endmag_{self.id}"
        self.tag_attack = f"insp_attack_{self.id}"
        self.tag_fade = f"insp_fade_{self.id}"
        
        label = "Inspector (Live)" if not clip else (clip.name or f"Clip {clip.type}")
        
        self.is_window = (parent is None)
        
        # Container Context Manager matching the type (Window or Tab)
        if self.is_window:
             self.container = dpg.window(label=label, tag=self.tag_tab, autosize=True, pos=pos)
        else:
             self.container = dpg.tab(label=label, tag=self.tag_tab, parent=parent, closable=(clip is not None))
             
        with self.container:
             dpg.add_text("Properties", tag=self.tag_title)
             dpg.add_separator()
             
             # Fields
             dpg.add_combo(label="Effect Type", items=["Sine","Square","Triangle","SawtoothUp","SawtoothDown","Constant","Spring","Damper","Inertia","Friction"], tag=self.tag_type, callback=self.on_change, user_data="type")
             dpg.add_input_float(label="Start (s)", tag=self.tag_start, callback=self.on_change, user_data="start")
             dpg.add_input_float(label="Duration (s)", tag=self.tag_dur, callback=self.on_change, user_data="dur")
             dpg.add_slider_int(label="Magnitude %", tag=self.tag_mag, max_value=100, callback=self.on_change, user_data="mag")
             
             dpg.add_separator()
             dpg.add_input_int(label="Frequency (Hz)", tag=self.tag_freq, min_value=1, max_value=5000, step=1, step_fast=10, callback=self.on_change, user_data="freq")
             dpg.add_checkbox(label="Enable Sweep", tag=self.tag_sweep, callback=self.on_change, user_data="sweep")
             dpg.add_input_int(label="End Freq (Hz)", tag=self.tag_freq_end, min_value=1, max_value=5000, step=1, step_fast=10, callback=self.on_change, user_data="freq_end")
             dpg.add_slider_int(label="Phase (deg)", tag=self.tag_phase, min_value=0, max_value=359, callback=self.on_change, user_data="phase")
             dpg.add_checkbox(label="Lock Phase", tag=self.tag_lock_phase, callback=self.on_change, user_data="lock_phase")

             dpg.add_separator()
             dpg.add_combo(label="Direction Mode", items=["Polar","Cartesian","Spherical"], tag=self.tag_dir_mode, callback=self.on_change, user_data="dir_mode")
             with dpg.group(tag=f"grp_polar_{self.id}"):
                 dpg.add_slider_int(label="Direction (deg)", tag=self.tag_angle, min_value=0, max_value=359, callback=self.on_change, user_data="angle")
                 dpg.add_input_int(label="Radius", tag=self.tag_radius, callback=self.on_change, user_data="radius")
             with dpg.group(tag=f"grp_cart_{self.id}"):
                 dpg.add_input_int(label="X", tag=self.tag_x, callback=self.on_change, user_data="x")
                 dpg.add_input_int(label="Y", tag=self.tag_y, callback=self.on_change, user_data="y")
                 dpg.add_input_int(label="Z", tag=self.tag_z, callback=self.on_change, user_data="z")
             with dpg.group(tag=f"grp_spherical_{self.id}"):
                 dpg.add_input_int(label="Yaw", tag=self.tag_yaw, callback=self.on_change, user_data="yaw")
                 dpg.add_input_int(label="Pitch", tag=self.tag_pitch, callback=self.on_change, user_data="pitch")
                 dpg.add_input_int(label="Distance", tag=self.tag_distance, callback=self.on_change, user_data="distance")

             dpg.add_separator()
             with dpg.group(tag=f"grp_ramp_{self.id}"):
                 dpg.add_input_int(label="Start Magnitude", tag=self.tag_start_mag, callback=self.on_change, user_data="start_mag")
                 dpg.add_input_int(label="End Magnitude", tag=self.tag_end_mag, callback=self.on_change, user_data="end_mag")

             dpg.add_separator()
             dpg.add_input_int(label="Attack (ms)", tag=self.tag_attack, callback=self.on_change, user_data="attack")
             dpg.add_input_int(label="Fade (ms)", tag=self.tag_fade, callback=self.on_change, user_data="fade")
             # if not clip:
             #     dpg.add_button(label="Open in New Tab", callback=self.duplicate_to_tab)
    
    def get_target_clip(self):
        if self.clip: return self.clip
        return self.app.sequencer.selected_clip

    def update(self):
        clip = self.get_target_clip()
        if not clip:
            # Disable or clear?
            # dpg.disable_item(self.tag_tab) # Can't disable tab content easily?
            dpg.set_value(self.tag_title, "No Selection")
            return
        
        dpg.set_value(self.tag_title, f"Clip: {clip.type} ({clip.id[:4]})")
        
        # Update Values (Only if not active to allow typing?)
        # Simple check: dpg.is_item_active
        
        def safe_set(tag, val):
            if not dpg.is_item_active(tag):
                dpg.set_value(tag, val)
        
        # Name update removed from here (handled via Right Click)

        
        safe_set(self.tag_start, clip.start_time)
        safe_set(self.tag_dur, clip.duration)
        safe_set(self.tag_mag, round((clip.magnitude / 32767.0) * 100))
        safe_set(self.tag_freq, clip.frequency)
        safe_set(self.tag_sweep, clip.sweep_enabled)
        safe_set(self.tag_phase, int(max(0, min(359, getattr(clip, 'start_phase', 0)))))
        safe_set(self.tag_lock_phase, getattr(clip, 'lock_phase', False))
        safe_set(self.tag_type, clip.type)
        safe_set(self.tag_dir_mode, clip.direction_mode)
        safe_set(self.tag_angle, int(max(0, min(359, getattr(clip, 'angle', 90)))))
        safe_set(self.tag_radius, getattr(clip, 'radius', 1))
        safe_set(self.tag_x, getattr(clip, 'x', 1))
        safe_set(self.tag_y, getattr(clip, 'y', 0))
        safe_set(self.tag_z, getattr(clip, 'z', 0))
        safe_set(self.tag_yaw, getattr(clip, 'yaw', 0))
        safe_set(self.tag_pitch, getattr(clip, 'pitch', 0))
        safe_set(self.tag_distance, getattr(clip, 'distance', 1))
        safe_set(self.tag_start_mag, getattr(clip, 'start_mag', -10000))
        safe_set(self.tag_end_mag, getattr(clip, 'end_mag', 10000))
        safe_set(self.tag_attack, getattr(clip, 'attack_length', 0))
        safe_set(self.tag_fade, getattr(clip, 'fade_length', 0))
        
        if clip.type == "Sine":
            dpg.show_item(self.tag_sweep)
            if clip.sweep_enabled:
                dpg.show_item(self.tag_freq_end)
                safe_set(self.tag_freq_end, clip.frequency_end)
            else:
                dpg.hide_item(self.tag_freq_end)
        else:
             dpg.hide_item(self.tag_sweep)
             dpg.hide_item(self.tag_freq_end)

        # Direction group visibility
        mode = clip.direction_mode or "Polar"
        dpg.configure_item(f"grp_polar_{self.id}", show=(mode == "Polar"))
        dpg.configure_item(f"grp_cart_{self.id}", show=(mode == "Cartesian"))
        dpg.configure_item(f"grp_spherical_{self.id}", show=(mode == "Spherical"))

        # Ramp-only fields
        dpg.configure_item(f"grp_ramp_{self.id}", show=(clip.type == "Ramp"))

    def on_change(self, sender, app_data, user_data):
        clip = self.get_target_clip()
        if not clip: return
        
        param = user_data
        param = user_data
        if param == "start": clip.start_time = max(0.0, app_data)
        elif param == "dur": clip.duration = max(0.01, app_data)
        elif param == "mag": 
            val = max(0, min(100, app_data))
            clip.magnitude = int((val / 100.0) * 32767)
        elif param == "freq": 
            if app_data < 1: 
                dpg.set_value(sender, 1)
                clip.frequency = 1
            else:
                clip.frequency = app_data
        elif param == "phase": clip.start_phase = max(0, min(359, app_data))
        elif param == "lock_phase": clip.lock_phase = bool(app_data)
        elif param == "freq_end": 
             if app_data < 1:
                 dpg.set_value(sender, 1)
                 clip.frequency_end = 1
             else:
                 clip.frequency_end = app_data
        elif param == "sweep": 
            clip.sweep_enabled = app_data
            self.update() # Refresh visibility immediately
        elif param == "type":
            clip.type = app_data
            self.update()
        elif param == "dir_mode":
            clip.direction_mode = app_data
            self.update()
        elif param == "angle": clip.angle = max(0, min(359, app_data))
        elif param == "radius": clip.radius = max(0, app_data)
        elif param == "x": clip.x = app_data
        elif param == "y": clip.y = app_data
        elif param == "z": clip.z = app_data
        elif param == "yaw": clip.yaw = max(0, min(360, app_data))
        elif param == "pitch": clip.pitch = max(0, min(360, app_data))
        elif param == "distance": clip.distance = max(0, app_data)
        elif param == "start_mag": clip.start_mag = app_data
        elif param == "end_mag": clip.end_mag = app_data
        elif param == "attack": clip.attack_length = max(0, app_data)
        elif param == "fade": clip.fade_length = max(0, app_data)
            
    def duplicate_to_tab(self, sender, app_data):
        clip = self.get_target_clip()
        if clip:
            self.app.create_floating_inspector(clip)

# --- Application ---
class MotorBusterNativeApp:
    UPDATE_LENGTH_BUFFER_MS = 10
    DRAG_ACTIVE_FPS = 60
    def __init__(self):
        self.sequencer = MotorBusterSequencer()
        self.sequencer.is_scrubbing = False # New state for playhead dragging
        self.log_items = []
        self.fps_frames = 0
        self.fps_last_time = time.time()
        self.renaming_track_idx = -1
        self.renaming_track_idx = -1
        self.drag_target_track_idx = -1 # For visual highlight
        self.inspectors = [] # List of InspectorPanel instances
        self.api_log_items = []
        self.resize_threshold_px = 12.0
        self._console_opened = False
        self.sweep_markers = []  # [{"time": float, "phase": int}]
        self.wheel_graph_height = 150
        self._wheel_graph_visible = True
        self.wheel_graph_gain = 1.0
        self.wheel_history = []
        self._last_live_wheel_norm = 0.0 # Live value for dot, separate from history
        self.wheel_history_limit = None
        self.wheel_axis_scale = 32767.0
        self.wheel_graph_auto_scale = True
        self.force_history = []  # New: Command force history
        self._force_graph_visible = False # Toggle
        self.hide_playhead_until_next_sample = False
        self.manual_timebase_override = False
        self.manual_timebase_value = None
        self.show_redlines = True
        self.mouse_left_button_down = False
        self.highlighted_clip_ids = set()
        self.multi_selected_clips = []
        self.multi_select_box_active = False
        self.multi_select_box_start = None
        self.multi_select_box_end = None
        self._highlight_drag_last_clip_id = None
        self._blocking_hover_tags = ("panel_inspector", "panel_log")
        self._hover_configs = {
            "panel_inspector": {
                "default": {"bg": (22, 22, 28), "border": (58, 62, 76)},
                "hover": {"bg": (51, 67, 92), "border": (120, 180, 255)},
            },
            "panel_log": {
                "default": {"bg": (22, 22, 28), "border": (58, 62, 76)},
                "hover": {"bg": (51, 67, 92), "border": (120, 180, 255)},
            },
            "timeline_scroll": {
                "default": {"bg": (16, 16, 20), "border": (40, 44, 56)},
                "hover": {"bg": (255, 249, 184), "border": (225, 190, 0)},
            },
            "Main": {
                "default": {"bg": (18, 18, 24), "border": (48, 56, 70)},
                "hover": {"bg": (44, 75, 135), "border": (120, 180, 255)},
            },
        }
        self._mouse_status_tag = "txt_mouse_status"
        self._mouse_status_visible = True
        self._drag_fps_override_hz = 0
        self._pending_scroll_x = None # For deferred zoom scrolling
        self._last_logged_freq_output = None
        
        # Serial Port State
        self.serial_ports = []
        self.serial_selected_port = None
        self.serial_baud = 115200
        self.serial_connected = False
        self.serial_timing_data = {}
        # Manual command selector state (Cmd tab)
        self._cmd_expects_response = False
        self._cmd_sent_time = 0.0
        self._cmd_timeout = 5.0
        
        # Diagnostic state (still referenced by UI)
        self.freeze_test_active = False
        self._should_trigger_freeze_now = False
        self._freeze_cycle_phase = "pause"
        self._freeze_cycle_phase_end = 0.0
        self._probe_thread = None
        self._probe_feedback_event = threading.Event()
        self._probe_feedback_result = None
        self._probe_streamed: Optional[bool] = None
        
        # Pre-load Cursors
        self.cursor_hand = ctypes.windll.user32.LoadCursorW(0, 32649) # IDC_HAND

        dpg.create_context()
        self.load_fonts()
        
        # Load User Settings (overrides defaults like show_redlines)
        self.load_settings()

        self.backend_mode = engine.get_backend_mode()
        
        self.setup_ui()
        # Theme is applied in run() to ensure proper initialization
        # self.apply_theme("Dark Mode")
        
        # Init Engine
        try:
            self.log("Initializing Haptic Subsystem...")
            engine.init_sdl()
        except Exception as e:
            self.log(f"SDL Init Error: {e}")


        self._ensure_api_console()
            
        # Playback State: { track_index: {'effect_id': -1, 'clip_id': None} }
        self.track_states = {}

        # Statistics
        self.stats_peak = 0.0
        self.stats_min = 0.0
        self.stats_sum = 0.0
        self.stats_count = 0

        # Initialize sweep engine callbacks
        sweep_engine.set_callbacks(
            on_send_effect=self._sweep_engine_send_effect,
            on_send_stop=self._sweep_engine_send_stop,
            on_progress=self._sweep_engine_progress,
            on_complete=self._sweep_engine_complete,
            on_log=self._sweep_engine_log,
        )

    @staticmethod
    def _clip_is_infinite(clip: Clip | None) -> bool:
        if not clip:
            return False
        duration = clip.duration
        return duration <= 0.0 or math.isinf(duration)


    # --- Clip helpers ---
    def _snap_to_edges(self, track: Track, clip: Clip, candidate_time: float, snap_px: float = 8.0) -> float:
        """Snap a time position to the nearest edge of neighboring clips within a pixel threshold."""
        threshold_s = snap_px / max(1.0, self.sequencer.zoom_x)
        best_time = candidate_time
        best_delta = threshold_s

        for other in track.clips:
            if other is clip:
                continue
            for edge in (other.start_time, other.start_time + other.duration):
                delta = abs(edge - candidate_time)
                if delta < best_delta:
                    best_delta = delta
                    best_time = edge

        return best_time

    def _avoid_overlap_on_drag(self, track: Track, clip: Clip, candidate_start: float) -> float:
        """Shift the dragged clip so it no longer overlaps neighbors, preserving duration."""
        start = max(0.0, candidate_start)
        end = start + clip.duration

        # Iterate until stable to avoid overlaps
        for _ in range(10):
            adjusted = False
            for other in track.clips:
                if other is clip:
                    continue
                o_start = other.start_time
                o_end = other.start_time + other.duration
                overlaps = not (end <= o_start or start >= o_end)
                if overlaps:
                    # Decide direction based on where we came from
                    if start >= o_start:
                        start = o_end
                    else:
                        start = max(0.0, o_start - clip.duration)
                    end = start + clip.duration
                    adjusted = True
                    break
            if not adjusted:
                break

        return start

    def _limit_right_resize(self, track: Track, clip: Clip, desired_dur: float) -> float:
        """Clamp right-edge resize so it does not pass the next clip and snaps to near edges."""
        next_start = None
        for other in track.clips:
            if other is clip:
                continue
            if other.start_time >= clip.start_time:
                if next_start is None or other.start_time < next_start:
                    next_start = other.start_time

        desired_end = clip.start_time + desired_dur
        if next_start is not None:
            desired_end = min(desired_end, next_start)

        desired_end = self._snap_to_edges(track, clip, desired_end)
        if next_start is not None and desired_end > next_start:
            desired_end = next_start

        return max(0.1, desired_end - clip.start_time)

    def _limit_left_resize(self, track: Track, clip: Clip, desired_start: float, fixed_end: float) -> float:
        """Clamp left-edge resize so it does not pass the previous clip and snaps to near edges."""
        prev_end = None
        for other in track.clips:
            if other is clip:
                continue
            o_end = other.start_time + other.duration
            if o_end <= fixed_end:
                if prev_end is None or o_end > prev_end:
                    prev_end = o_end

        candidate = max(0.0, desired_start)
        candidate = self._snap_to_edges(track, clip, candidate)

        if prev_end is not None and candidate < prev_end:
            candidate = prev_end

        # Prevent inversion
        if candidate > fixed_end - 0.1:
            candidate = max(0.0, fixed_end - 0.1)

        return candidate

    def _get_contiguous_chain_end(self, track: Track, start_clip: Clip) -> float:
        """
        Finds the end time of the contiguous chain of clips starting from start_clip.
        Clips are considered contiguous if gap < 1ms (approx).
        """
        current_clip = start_clip
        chain_end = current_clip.start_time + current_clip.duration
        
        # Iterate to find next neighbor until chain breaks
        while True:
            found_next = False
            for other in track.clips:
                if other is current_clip: continue
                
                # Check if 'other' starts exactly where 'current' ends (with small tolerance)
                # Tolerance: 0.001s (1ms)
                if abs(other.start_time - chain_end) < 0.005: # 5ms tolerance for "touching"
                    chain_end = other.start_time + other.duration
                    current_clip = other
                    found_next = True
                    break
            if not found_next:
                break
                
        return chain_end

    # --- Waveform helpers ---
    def _wave_amplitude(self, clip: Clip, t: float) -> float:
        """Return signed amplitude at time t (seconds) for display purposes."""
        mag = clip.magnitude
        
        # Non-periodic / Constant-like types for visualization
        if clip.type in ("Constant", "Spring", "Damper", "Inertia", "Friction"):
            return mag
        
        # Periodic types
        freq = max(1e-6, clip.frequency)
        
        # Handle Sweep for periodic types
        is_sweep = getattr(clip, 'sweep_enabled', False)
        
        phase = 0.0
        if is_sweep:
            start_f = clip.frequency
            end_f = getattr(clip, 'frequency_end', start_f)
            # Chirp phase
            k = (end_f - start_f) / max(1e-6, clip.duration)
            phase = 2 * math.pi * (start_f * t + 0.5 * k * t * t)
        else:
            phase = 2 * math.pi * freq * t
            
        # Add start phase
        start_phase_rad = math.radians(getattr(clip, "start_phase", 0))
        total_phase = (phase + start_phase_rad) % (2 * math.pi)
        norm_phase = total_phase / (2 * math.pi) # 0.0 to 1.0
        
        if clip.type == "Square":
            return mag * (1.0 if math.sin(total_phase) >= 0 else -1.0)
            
        elif clip.type == "Triangle":
            # 0->1 (0-0.25), 1->-1 (0.25-0.75), -1->0 (0.75-1.0)
            # Matched to Sine phase (0 at 0, peak at pi/2)
            if norm_phase < 0.25:
                return mag * (4.0 * norm_phase)
            elif norm_phase < 0.75:
                return mag * (2.0 - 4.0 * norm_phase)
            else:
                return mag * (4.0 * norm_phase - 4.0)
                
        elif clip.type in ("Sawtooth", "SawtoothUp"):
             # Shifted to match Sine: Start at 0, go up
             # Original was -1 + 2*norm (Starts at -1)
             # New: Starts at 0, wraps at 0.5 (180 deg) ? 
             # No, standard Saw is -1 to 1.
             # We want 0 crossing at Phase 0.
             shifted_norm = (norm_phase + 0.5) % 1.0
             return mag * (-1.0 + 2.0 * shifted_norm)
             
        elif clip.type == "SawtoothDown":
             # Shifted to match Sine 0-crossing (but falling)
             shifted_norm = (norm_phase + 0.5) % 1.0
             return mag * (1.0 - 2.0 * shifted_norm)

        # Default: Sine
        return mag * math.sin(total_phase)

    def _clip_wave_points(self, clip: Clip, x_start: float, width: float, y_top: float, y_bottom: float, samples: int = 0):
        # Precise Peak-Detection for Aliasing
        points = []
        y_mid = (y_top + y_bottom) / 2.0
        amp_span = (y_bottom - y_top) * 0.45 
        mag_max = 32767.0 # Normalize against full scale, so lower magnitude = smaller wave
        
        # Calculate visualization scale based on clip magnitude
        clip_scale = clip.magnitude / mag_max

        if width <= 0: return []

        pixels = max(1, int(width))
        max_samples = 5000
        
        # Respect requested sample count if provided, otherwise default to 1 pixel per sample
        if samples > 0:
            sample_count = min(samples, max_samples)
        else:
            sample_count = min(pixels, max_samples)

        duration = max(clip.duration, 1e-6)
        step_t = duration / max(1, sample_count)
        step_x = width / max(1, sample_count)

        freq = max(clip.frequency, getattr(clip, 'frequency_end', clip.frequency))
        is_aliasing = False
        if freq > 0 and (1.0/freq) < step_t * 2.5:
             is_aliasing = True

        for i in range(sample_count):
            t0 = i * step_t
            t1 = (i + 1) * step_t
            
            local_min = 0.0
            local_max = 0.0

            if is_aliasing:
                local_min = -1.0 * clip_scale
                local_max = 1.0 * clip_scale
            else:
                local_min = 1.0 # Init inverted
                local_max = -1.0
                
                # Check 8 internal points + edges
                for k in range(8):
                    ft = t0 + (t1 - t0) * (k / 7.0)
                    val = self._wave_amplitude(clip, ft) / mag_max
                    if val < local_min: local_min = val
                    if val > local_max: local_max = val

            y_l = y_mid - max(-1.0, min(1.0, local_min)) * amp_span
            y_h = y_mid - max(-1.0, min(1.0, local_max)) * amp_span
            
            x = x_start + i * step_x
            points.append([x, y_l])
            points.append([x, y_h])
            
        return points

    def log(self, message):
        self.log_items.append(message)
        if len(self.log_items) > 50: self.log_items.pop(0)
        if dpg.does_item_exist("log_list"):
            dpg.configure_item("log_list", items=self.log_items)

    def log_api(self, action, payload):
        """Record haptic API interactions; mirror only to API console/buffer (not main UI log)."""
        preview = payload
        if isinstance(payload, dict):
            preview = dict(payload)
            if "length_us" in preview:
                preview.pop("length_ms", None)
        try:
            preview = json.dumps(preview) if not isinstance(preview, str) else preview
        except Exception:
            preview = str(preview)
        entry = f"API {action}: {preview}"
        print(entry)

        # Keep dedicated API buffer (in case we re-add a panel later)
        self.api_log_items.append(entry[:500])
        if len(self.api_log_items) > 200:
            self.api_log_items.pop(0)

    @staticmethod
    def _s16_to_di(magnitude_s16: int) -> int:
        """Map signed-16 clip magnitude domain into DirectInput 0..10000."""
        mag = abs(int(magnitude_s16))
        mag = max(0, min(32767, mag))
        return int(round(mag * 10000.0 / 32767.0))

    def _ensure_api_console(self):
        """Open a dedicated console for API logs (Windows only)."""
        if self._console_opened:
            return
        try:
            ctypes.windll.kernel32.AllocConsole()
            sys.stdout = open("CONOUT$", "w")
            sys.stderr = sys.stdout
            self._console_opened = True
            print("API console initialized")
        except Exception as e:
            # Fallback: keep stdout as-is; API logs still appear in panel
            self.log(f"API console unavailable: {e}")
            
    # --- Project Management ---
    def save_project_to_file(self, path):
        data = self.sequencer.to_dict()
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            self.log(f"Project Saved: {os.path.basename(path)}")
        except Exception as e:
            self.log(f"Save Failed: {e}")

    def load_project_from_file(self, path):
        try:
            if not os.path.exists(path):
                self.log("File not found.")
                return
            with open(path, "r") as f:
                data = json.load(f)
            self.sequencer.load_from_dict(data)
            self.highlighted_clip_ids.clear()
            self.log(f"Loaded: {os.path.basename(path)}")
        except Exception as e:
            self.log(f"Load Failed: {e}")

    def action_save_file(self, sender, app_data):
        # app_data['file_path_name'] is the full path
        path = app_data.get('file_path_name')
        if path:
             # Ensure extension
             if not path.endswith(".motorbuster"): path += ".motorbuster"
             self.save_project_to_file(path)

    def action_load_file(self, sender, app_data):
        path = app_data.get('file_path_name')
        if path:
             self.load_project_from_file(path)

    def scan_devices(self, sender=None, app_data=None):
        self.log("Scanning devices...")
        devs = engine.list_devices()
        items = [f"{d.name} (#{d.index})" for d in devs]
        dpg.configure_item("device_combo", items=items)
        if items: 
            dpg.set_value("device_combo", items[0])
            self.connect_device_by_name(items[0]) # Auto-connect on scan/startup
        else:
            dpg.set_value("device_combo", "No Devices Found")

    def _get_device_guid(self) -> Optional[str]:
        """Get the GUID of the currently connected device."""
        try:
            dev = engine.get_connected_device()
            if dev and hasattr(dev, 'guid'):
                return str(dev.guid)
        except:
            pass
        return None

    def _get_torque_for_device(self, name: str) -> float:
        """Detect max torque via GUID lookup + user override persistence."""
        # 1. Check user overrides (persisted GUID → Nm)
        guid = self._get_device_guid()
        if guid and hasattr(self, '_device_torque_overrides') and guid in self._device_torque_overrides:
            return self._device_torque_overrides[guid]

        # 2. GUID-based accurate lookup
        GUID_TORQUE_DB = {
            # Simucube
            "{AEB1D5C2-EA62-44F0-A05C-80E5D9E306D2}": 32.0,   # SC2 Ultimate
            "{AEB1D5C2-EA62-44F0-A05C-80E5D9E306D1}": 25.0,   # SC2 Pro
            "{AEB1D5C2-EA62-44F0-A05C-80E5D9E306D0}": 17.0,   # SC2 Sport
            # Fanatec
            "{0007-0000-0000-0000-504944564944}": 25.0,        # DD2
            "{0006-0000-0000-0000-504944564944}": 20.0,        # DD1
            "{0020-0000-0000-0000-504944564944}": 8.0,         # CSL DD
            # Moza
            "{0005-0000-0000-0000-504944564944}": 21.0,        # R21
            "{0004-0000-0000-0000-504944564944}": 16.0,        # R16
            "{0003-0000-0000-0000-504944564944}": 12.0,        # R12
            "{0002-0000-0000-0000-504944564944}": 9.0,         # R9
            "{0001-0000-0000-0000-504944564944}": 5.5,         # R5
        }
        
        if guid and guid.upper() in GUID_TORQUE_DB:
            return GUID_TORQUE_DB[guid.upper()]

        # 3. Name-based fallback (for devices without known GUIDs)
        name_lower = name.lower()
        NAME_DB = {
            "simucube 2 ultimate": 32.0,
            "simucube 2 pro": 25.0,
            "simucube 2 sport": 17.0,
            "fanatec dd2": 25.0,
            "fanatec dd1": 20.0,
            "fanatec csl dd": 8.0,
            "fanatec gt dd pro": 8.0,
            "moza r21": 21.0,
            "moza r16": 16.0,
            "moza r12": 12.0,
            "moza r9": 9.0,
            "moza r5": 5.5,
            "logitech g pro": 11.0,
            "thrustmaster t818": 10.0,
            "vrs directforce pro": 20.0,
            "simagic alpha ultimate": 23.0,
            "simagic alpha": 15.0,
            "simagic alpha mini": 10.0,
            "asetek invicta": 27.0,
            "asetek forte": 18.0,
            "asetek la prima": 12.0,
            "conspit ares platinum": 20.0,
            "conspit ares apex": 8.0,
            "conspit ares": 10.0,
        }
        best_val = 0.0
        max_len = 0
        for key, val in NAME_DB.items():
            if key in name_lower:
                if len(key) > max_len:
                    max_len = len(key)
                    best_val = val
        return best_val

    def _save_device_torque_override(self, guid: str, torque_nm: float):
        """Persist user torque override to settings file keyed by device GUID."""
        if not hasattr(self, '_device_torque_overrides'):
            self._device_torque_overrides = {}
        self._device_torque_overrides[guid] = torque_nm
        # Save to settings
        try:
            import json, os
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_settings.json")
            settings = {}
            if os.path.exists(path):
                with open(path, 'r') as f:
                    settings = json.load(f)
            settings["device_torque_overrides"] = self._device_torque_overrides
            with open(path, 'w') as f:
                json.dump(settings, f, indent=4)
        except Exception as e:
            self.log(f"Failed to save torque override: {e}")

    def connect_device_by_name(self, name):
         try:
             idx = int(name.split("#")[-1].replace(")", ""))
             hwnd = self._get_viewport_hwnd()
             if not hwnd:
                 import time
                 for _ in range(10):
                     time.sleep(0.05)
                     hwnd = self._get_viewport_hwnd()
                     if hwnd:
                         break
             engine.set_directinput_hwnd(hwnd)
             if engine.connect_device(idx):
                 dpg.set_value("status_text", "Status: Connected")
                 dpg.configure_item("status_text", color=self.theme_colors.get("text_green", (0, 255, 0)))
                 self._last_logged_freq_output = None
                 
                 # Ensure device starts in a neutral state
                 engine.stop_effect()
                 self.log_api("stop_effect", {"scope": "all"})
                 
                 # Reset previous effect states as we have a new device
                 for k in self.track_states:
                     self.track_states[k] = {
                         'effect_id': -1,
                         'clip_id': None,
                         'clip_type': None,
                         'last_sweep_update_local': None,
                         'last_sent_freq': None,
                         'last_mag': None,
                         'last_freq': None,
                         'creation_pending': False,
                     }
                 
                 # Auto-Detect Torque
                 detected_torque = self._get_torque_for_device(name)
                 if detected_torque > 0.0:
                     if dpg.does_item_exist("input_max_torque"):
                         dpg.set_value("input_max_torque", detected_torque)
                         self.log(f"Auto-Detected Torque: {detected_torque} Nm")
                 
                 # Auto-Detect Polling Rate
                 detected_rate = engine.detect_device_poll_rate(test_duration_s=0.3)
                 target_rate = engine.get_effective_update_rate()
                 if dpg.does_item_exist("poll_rate_input"):
                     dpg.set_value("poll_rate_input", target_rate)
                 self.log(f"Poll Rate: {detected_rate}Hz detected, using {target_rate}Hz")
                 
         except: pass

    def on_device_selected(self, sender, app_data):
        """Callback for device combo: auto-connect when user selects a new device."""
        if app_data and "No Devices" not in app_data:
            self.connect_device_by_name(app_data)

    def connect_callback(self):
        val = dpg.get_value("device_combo")
        
        # If no device selected or list empty, try scanning first
        if not val or "No Devices" in val:
            self.scan_devices()
            val = dpg.get_value("device_combo")
            
        if val and "No Devices" not in val:
            self.connect_device_by_name(val)

    def run_diag_test(self):
        """Triggers the strict HW Periodic test with integrated freeze to prove hardware independence."""
        self.log("Starting Force HW Periodic Test (Strict HW, Integrated Freeze)...")
        # Integrated Freeze is now handled INSIDE engine.diag_test_hw_sine via 1s sleep
        # but we can still stop the UI thread here if we want simultaneous freeze.
        # Actually, let's let the engine handle the 'PC freeze' simulation.
        threading.Thread(target=engine.diag_test_hw_sine, daemon=True).start()

    # --- HW Capability Mode ---
    def start_ffb_probe(self):
        dpg.set_value("txt_probe_status", "Status: Ready")
        dpg.set_value("progress_probe", 0.0)
        dpg.set_value("txt_probe_log", "Waiting to start...")
        dpg.configure_item("group_probe_results", show=False)
        dpg.configure_item("group_probe_feedback", show=False)
        dpg.configure_item("group_probe_controls", show=True)
        if dpg.does_item_exist("chk_probe_streamed"):
            dpg.set_value("chk_probe_streamed", self._probe_streamed is True)
        if dpg.does_item_exist("chk_probe_nonstreamed"):
            dpg.set_value("chk_probe_nonstreamed", self._probe_streamed is False)
        self._update_probe_start_enabled()
        dpg.show_item("win_ffb_probe")

    def _exec_ffb_probe(self):
        if self._probe_streamed is None:
            self.log("Select Streamed or Non-streamed updates before starting the test.")
            return
        if dpg.does_item_exist("btn_probe_start"):
            dpg.configure_item("btn_probe_start", enabled=False)
        dpg.set_value("txt_probe_status", "Status: Running...")
        
        def probe_wrapper():
            def feedback_cb(name, msg):
                self._probe_feedback_result = None
                self._probe_feedback_event.clear()
                
                # Update UI to ask user
                dpg.set_value("txt_probe_feedback_msg", msg)
                dpg.configure_item("group_probe_feedback", show=True)
                
                # Wait for user input
                self._probe_feedback_event.wait()
                
                dpg.configure_item("group_probe_feedback", show=False)
                return self._probe_feedback_result

            # Connect engine's HWP log callback to our UI
            def ui_hwp_log(msg):
                curr = dpg.get_value("txt_probe_log")
                if curr == "Waiting to start...":
                    curr = ""
                dpg.set_value("txt_probe_log", f"{curr}\n{msg}")
            
            engine._probe_log_callback = ui_hwp_log

            def progress_cb(effect_name, phase, remaining_s, test_idx=1, test_total=1):
                label = f"Test {test_idx}/{test_total}: {effect_name}"
                if phase == "DONE":
                    dpg.set_value("txt_probe_status", f"Status: {label} complete")
                    ui_hwp_log(f"{label} complete")
                else:
                    dpg.set_value("txt_probe_status", f"Status: {label} {phase} ({remaining_s}s)")
                    ui_hwp_log(f"{label} {phase}: {remaining_s}s remaining")
            
            try:
                if dpg.does_item_exist("chk_probe_streamed") and dpg.does_item_exist("chk_probe_nonstreamed"):
                    self._probe_streamed = bool(dpg.get_value("chk_probe_streamed"))
                report = engine.run_hardware_effects_probe(
                    feedback_callback=feedback_cb,
                    progress_callback=progress_cb,
                    stream_updates=self._probe_streamed,
                )
                
                # Finalize UI
                dpg.set_value("txt_probe_status", "Status: Completed")
                dpg.set_value("progress_probe", 1.0)
                dpg.configure_item("group_probe_results", show=True)
                dpg.configure_item("group_probe_controls", show=False)
                
                rec = report.get("recommendation", "N/A")
                if "tests" in report and "sine" in report["tests"]:
                     rec = f"Sine Status: {report['tests']['sine']}"
                
                # Use engine's cached result for better recommendation display
                last_res = engine.last_probe_result
                dpg.set_value("txt_probe_recommendation", f"Recommendation: {last_res.get('recommendation', 'N/A')}")
                
            except Exception as e:
                self.log(f"Probe Error: {e}")
                ui_hwp_log(f"HWP: Probe Error: {e}")
            finally:
                engine._probe_log_callback = None

        self._probe_thread = threading.Thread(target=probe_wrapper, daemon=True)
        self._probe_thread.start()

    def _on_probe_feedback(self, result: str):
        self._probe_feedback_result = result
        self._probe_feedback_event.set()

    def _update_probe_start_enabled(self):
        enabled = self._probe_streamed is not None
        if dpg.does_item_exist("btn_probe_start"):
            dpg.configure_item("btn_probe_start", enabled=enabled)

    def _on_probe_stream_mode(self, mode: str):
        if mode == "streamed":
            self._probe_streamed = True
        elif mode == "nonstreamed":
            self._probe_streamed = False
        if dpg.does_item_exist("chk_probe_streamed"):
            dpg.set_value("chk_probe_streamed", self._probe_streamed is True)
        if dpg.does_item_exist("chk_probe_nonstreamed"):
            dpg.set_value("chk_probe_nonstreamed", self._probe_streamed is False)
        self._update_probe_start_enabled()

    def _close_ffb_probe(self):
        dpg.hide_item("win_ffb_probe")
        # Save report automatically
        engine.diag_export_report("motorbuster_ffb_report.txt")
        self.log("FFB Probe Completed. Report saved to motorbuster_ffb_report.txt")
        
        # Apply recommendation global default? 
        # For now, we'll just log it. The user can see it in clips.
        rec = engine.recommendation_hw_sine
        self.log(f"Global Recommendation: {'HARDWARE' if rec else 'NOT SUPPORTED'} SINE")

    # --- Serial Port Methods ---
    def _scan_serial_ports(self, sender=None, app_data=None):
        """Scan available serial ports and update the combo."""
        ports = scan_ports()
        self.serial_ports = ports
        items = [p["description"] for p in ports]
        if items:
            dpg.configure_item("serial_port_combo", items=items)
            dpg.set_value("serial_port_combo", items[0])
            self.serial_selected_port = ports[0]["port"]
            self.log(f"Found {len(ports)} serial port(s)")
        else:
            dpg.configure_item("serial_port_combo", items=["No ports found"])
            dpg.set_value("serial_port_combo", "No ports found")
            self.log("No serial ports found")

    def _on_serial_port_selected(self, sender, app_data):
        """Called when user selects a port from the combo."""
        for p in self.serial_ports:
            if p["description"] == app_data:
                self.serial_selected_port = p["port"]
                break

    def _on_serial_baud_changed(self, sender, app_data):
        """Called when baud rate changes."""
        try:
            self.serial_baud = int(app_data)
        except:
            self.serial_baud = 115200

    def _connect_serial(self, sender=None, app_data=None):
        """Connect or disconnect the serial port."""
        if self.serial_connected:
            serial_manager.disconnect()
            self.serial_connected = False
            dpg.configure_item("btn_serial_connect", label="Connect")
            dpg.configure_item("txt_serial_status", color=(100, 100, 100))
            dpg.set_value("txt_serial_status", "COM: Offline")
            self.log("Serial disconnected")
            return

        port = self.serial_selected_port
        if not port:
            self.log("No serial port selected")
            return

        if serial_manager.connect(port, self.serial_baud):
            self.serial_connected = True
            dpg.configure_item("btn_serial_connect", label="Disconnect")
            dpg.configure_item("txt_serial_status", color=(100, 255, 100))
            dpg.set_value("txt_serial_status", f"COM: {port} ({self.serial_baud})")
            self.log(f"Serial connected to {port} @ {self.serial_baud} baud")
            serial_manager.set_response_callback(self._on_serial_response)
        else:
            self.log(f"Failed to connect to {port}")

    def _on_serial_response(self, data: dict):
        """Callback when serial response is received from MCU."""
        self.serial_timing_data = data
        mag_db = data.get('mag_db', 0.0)
        phase_deg = data.get('phase_deg', 0.0)
        if dpg.does_item_exist('txt_serial_timing'):
            dpg.set_value('txt_serial_timing', f'Mag = {mag_db:.2f} dB | Phase = {phase_deg:.2f}°')
            dpg.configure_item('txt_serial_timing', color=(100, 255, 100))
        self.log(f'Serial Response: Mag={mag_db:.2f} dB, Phase={phase_deg:.2f}°')

        # Also surface the response in the Cmd tab if waiting on a manual command
        if getattr(self, '_cmd_expects_response', False) and dpg.does_item_exist('txt_cmd_status'):
            dpg.set_value('txt_cmd_status', f'Response: cmd=0x{data.get("cmd", 0):02X} | Mag = {mag_db:.2f} dB | Phase = {phase_deg:.2f}°')
            self._cmd_expects_response = False

    # --- Manual Command Selector (Cmd tab) ---
    def _on_cmd_selector_change(self, sender, app_data):
        # Enable/disable parameter fields based on the selected command.
        key = (app_data or '').split(' ')[0].lower()
        needs_params = key in ('sine', 'constant')
        for tag in ('cmd_freq', 'cmd_mag_pct', 'cmd_max_torque', 'cmd_settle', 'cmd_ramp'):
            if not dpg.does_item_exist(tag):
                continue
            if needs_params:
                dpg.enable_item(tag)
            else:
                dpg.disable_item(tag)

    def _cmd_send(self, sender=None, app_data=None):
        # Send the selected firmware command from the Cmd tab.
        if not self.serial_connected:
            dpg.set_value('txt_cmd_status', 'Error: Serial not connected')
            return
        if sweep_engine.is_running:
            dpg.set_value('txt_cmd_status', 'Error: A sweep is running - press Stop first')
            return

        sel = dpg.get_value('cmd_selector') or ''
        key = sel.split(' ')[0].lower()
        cmd_hex = {'sine': '0x01', 'constant': '0x06', 'center': '0x08',
                   'scale': '0x09', 'selftest': '0xFE', 'stop': '0xFF'}.get(key, '0x??')
        expects_response = key in ('sine', 'constant', 'selftest')

        freq = max(1.0, dpg.get_value('cmd_freq'))
        mag_pct = dpg.get_value('cmd_mag_pct')
        settle = dpg.get_value('cmd_settle')
        ramp = dpg.get_value('cmd_ramp')
        max_torque = dpg.get_value('cmd_max_torque')
        mag = int(32767 * mag_pct / 100.0)

        if key == 'center':
            ok = serial_manager.send_center()
        elif key == 'scale':
            ok = serial_manager.send_scale()
        elif key == 'stop':
            ok = serial_manager.send_stop()
        elif key == 'selftest':
            ok = serial_manager.send_selftest()
        else:  # sine / constant
            ok = serial_manager.send_effect(key, freq, mag, settle, ramp, max_torque)

        if not ok:
            dpg.set_value('txt_cmd_status', 'Send failed (not connected?)')
            return

        if expects_response:
            if key in ('sine', 'constant'):
                self._cmd_timeout = (1.0 / freq) * (settle + ramp + 8) + 1.0
            else:  # selftest
                self._cmd_timeout = 5.0
            self._cmd_expects_response = True
            self._cmd_sent_time = time.time()
            dpg.set_value('txt_cmd_status', f'Sent {key.upper()} ({cmd_hex}) - waiting for response...')
        else:
            self._cmd_expects_response = False
            dpg.set_value('txt_cmd_status', f'Sent {key.upper()} ({cmd_hex}) - no response expected')

    def _cmd_send_stop(self, sender=None, app_data=None):
        # Quick STOP button in the Cmd tab.
        if not self.serial_connected:
            dpg.set_value('txt_cmd_status', 'Error: Serial not connected')
            return
        ok = serial_manager.send_stop()
        self._cmd_expects_response = False
        dpg.set_value('txt_cmd_status', 'Sent STOP (0xFF)' if ok else 'Send failed')

    # --- Sweep Panel Methods ---
    def _on_sweep_torque_slider(self, sender, app_data):
        """Update the torque readout when slider moves."""
        pct = int(app_data) if app_data is not None else 0
        max_nm = 25.0
        if dpg.does_item_exist("sweep_max_torque"):
            max_nm = dpg.get_value("sweep_max_torque")
        actual_nm = max_nm * pct / 100.0
        if dpg.does_item_exist("txt_sweep_torque_readout"):
            dpg.set_value("txt_sweep_torque_readout", f"{pct}% ({actual_nm:.1f} Nm)")

    def _update_sweep_torque_display(self):
        """Update the torque display fields from the connected device."""
        if not dpg.does_item_exist("sweep_max_torque"):
            return
        detected_torque = 25.0
        try:
            name = dpg.get_value("device_combo")
            if name and "No Devices" not in name:
                detected_torque = self._get_torque_for_device(name)
                if detected_torque <= 0:
                    detected_torque = 25.0
        except:
            pass
        dpg.set_value("sweep_max_torque", detected_torque)
        self._on_sweep_torque_slider(None, dpg.get_value("sweep_torque_pct"))

    def _open_sweep_panel(self, sender=None, app_data=None):
        """Open the Create Sinestream Sweep dialog window."""
        # Delete stale cached window to ensure fresh UI
        if dpg.does_item_exist("win_sweep_v2"):
            dpg.delete_item("win_sweep_v2")

        width, height = 650, 580
        vp_w = dpg.get_viewport_width()
        vp_h = dpg.get_viewport_height()
        pos = [(vp_w - width) // 2, (vp_h - height) // 2]
        
        with dpg.window(tag="win_sweep_v2", label="Sinestream Sweep", width=width, height=height,
                        pos=pos, modal=False, no_resize=False, on_close=lambda: None):
            
            dpg.add_text("Frequency Range", color=(200, 200, 255))
            dpg.add_separator()

            with dpg.group(horizontal=True):
                dpg.add_text("Min (Hz):")
                dpg.add_input_float(tag="sweep_freq_min", default_value=10.0, width=100, step=0)
                dpg.add_spacer(width=20)
                dpg.add_text("Max (Hz):")
                dpg.add_input_float(tag="sweep_freq_max", default_value=500.0, width=100, step=0)
            with dpg.group(horizontal=True):
                dpg.add_text("No. of frequencies:")
                dpg.add_input_int(tag="sweep_num_freq", default_value=20, width=100, min_value=2, max_value=200)

            dpg.add_text("")
            dpg.add_text("Signal Configuration", color=(180, 180, 200))
            dpg.add_separator()

            # Max Torque (auto-detected, editable)
            with dpg.group(horizontal=True):
                dpg.add_text("Max Torque (Nm):")
                dpg.add_input_float(tag="sweep_max_torque", default_value=25.0, width=100, min_value=0.1, max_value=100.0, step=0)
                dpg.add_text("(auto-detected)", color=(140, 140, 140))

            # Torque Slider (0-100%) with live Nm readout
            with dpg.group(horizontal=True):
                dpg.add_text("Torque:")
                dpg.add_slider_int(tag="sweep_torque_pct", default_value=10, min_value=0, max_value=100, width=300,
                                   callback=self._on_sweep_torque_slider)
                dpg.add_text("10% (2.5 Nm)", tag="txt_sweep_torque_readout", color=(180, 220, 180))

            with dpg.group(horizontal=True):
                dpg.add_text("Settling periods:")
                dpg.add_input_int(tag="sweep_settling", default_value=2, width=80, min_value=0, max_value=4)
                dpg.add_spacer(width=15)
                dpg.add_text("Ramp periods:")
                dpg.add_input_int(tag="sweep_ramp", default_value=1, width=80, min_value=0, max_value=1)

            dpg.add_separator()

            # Frequency list text display and preview
            with dpg.group(horizontal=True):
                dpg.add_text("Frequencies:", color=(200, 200, 255))
                dpg.add_button(label="Preview", width=80, callback=self._sweep_preview_freqs)
            
            dpg.add_text("", tag="txt_sweep_freqs", wrap=580, color=(180, 220, 180))
            
            # Progress & Log area
            dpg.add_progress_bar(tag="sweep_progress", width=-1, default_value=0.0)
            dpg.add_text("Status: Ready", tag="txt_sweep_status", color=(200, 200, 200))

            # Action buttons
            with dpg.group(horizontal=True):
                dpg.add_button(label="Run Sweep", tag="btn_sweep_run", width=120, callback=self._sweep_run)
                dpg.add_button(label="Cancel", tag="btn_sweep_cancel", width=120, enabled=False, callback=self._sweep_cancel)
                dpg.add_button(label="Export JSON", tag="btn_sweep_export", width=120, enabled=False, callback=self._sweep_export)
                dpg.add_button(label="Close", width=80, callback=lambda: dpg.delete_item("win_sweep_v2"))

        # Initialize sweep engine callbacks
        sweep_engine.set_callbacks(
            on_send_effect=self._sweep_engine_send_effect,
            on_send_stop=self._sweep_engine_send_stop,
            on_progress=self._sweep_engine_progress,
            on_complete=self._sweep_engine_complete,
            on_log=self._sweep_engine_log,
        )

        # Set initial torque values from device detection
        self._update_sweep_torque_display()

    def _sweep_preview_freqs(self, sender=None, app_data=None):
        """Preview the generated frequency list."""
        config = self._sweep_get_config()
        if config is None:
            return
        freqs = config.get_frequencies()
        if len(freqs) > 50:
            shown = [f"{f:.2f}" for f in freqs[:25]] + ["..."] + [f"{f:.2f}" for f in freqs[-5:]]
        else:
            shown = [f"{f:.2f}" for f in freqs]
        preview = ", ".join(shown)
        if dpg.does_item_exist("txt_sweep_freqs"):
            dpg.set_value("txt_sweep_freqs", f"{len(freqs)} frequencies: [{preview}] Hz")

    def _sweep_get_config(self) -> Optional[SweepConfig]:
        """Read sweep panel UI values into a SweepConfig."""
        try:
            config = SweepConfig()
            config.freq_min_hz = dpg.get_value("sweep_freq_min")
            config.freq_max_hz = dpg.get_value("sweep_freq_max")
            config.num_frequencies = dpg.get_value("sweep_num_freq")
            
            # Always logarithmic spacing (hardcoded default)
            config.spacing = "logarithmic"

            # Compute amplitude from torque percentage
            torque_pct = dpg.get_value("sweep_torque_pct")
            config.amplitude = int(32767 * torque_pct / 100.0)

            # Max torque from UI
            config.max_torque_nm = dpg.get_value("sweep_max_torque")

            config.settling_periods = dpg.get_value("sweep_settling")
            config.ramp_periods = dpg.get_value("sweep_ramp")
            config.perform_filtering = True  # Always enabled
            
            return config
        except Exception as e:
            self.log(f"Sweep config error: {e}")
            return None

    def _sweep_run(self, sender=None, app_data=None):
        """Run the sweep with current UI config."""
        if not self.serial_connected:
            self.log("Serial not connected. Cannot run sweep without MCU connection.")
            return

        if sweep_engine.is_running:
            self.log("Sweep already running")
            return

        config = self._sweep_get_config()
        if config is None:
            return

        # Update UI state
        dpg.configure_item("btn_sweep_run", enabled=False)
        dpg.configure_item("btn_sweep_cancel", enabled=True)
        dpg.configure_item("btn_sweep_export", enabled=False)
        dpg.set_value("sweep_progress", 0.0)
        dpg.set_value("txt_sweep_status", "Status: Running...")

        # Route serial responses to sweep engine
        serial_manager.set_response_callback(sweep_engine.handle_response)

        # Start the sweep
        sweep_engine.run_sweep(config)

    def _sweep_cancel(self, sender=None, app_data=None):
        """Cancel the running sweep."""
        sweep_engine.cancel_sweep()
        dpg.set_value("txt_sweep_status", "Status: Cancelling...")

    def _sweep_export(self, sender=None, app_data=None):
        """Export sweep results."""
        sweep_engine.export_results("motorbuster_sweep_results.json")

    def _sweep_engine_send_effect(self, effect_type: str, freq_hz: float, magnitude: int,
                                  settle_periods: int = 2, ramp_periods: int = 1,
                                  max_torque_nm: float = 25.0) -> bool:
        """Called by sweep engine to send an effect over serial."""
        if self.serial_connected:
            return serial_manager.send_effect(effect_type, freq_hz, magnitude,
                                              settle_periods, ramp_periods, max_torque_nm)
        return False

    def _sweep_engine_send_stop(self):
        """Called by sweep engine to send stop."""
        if self.serial_connected:
            serial_manager.send_stop()

    def _sweep_engine_progress(self, current: int, total: int, label: str):
        """Called by sweep engine to report progress."""
        if dpg.does_item_exist("sweep_progress"):
            dpg.set_value("sweep_progress", current / max(1, total))
        if dpg.does_item_exist("txt_sweep_status"):
            dpg.set_value("txt_sweep_status", f"Status: {current}/{total} - {label}")

    def _sweep_engine_complete(self, data: SweepData):
        """Called by sweep engine on completion."""
        # Restore normal serial callback
        serial_manager.set_response_callback(self._on_serial_response)

        if dpg.does_item_exist("txt_sweep_status"):
            status = data.status.capitalize()
            valid = len([r for r in data.results if r.valid])
            dpg.set_value("txt_sweep_status", f"Status: {status} ({valid}/{len(data.results)} valid)")
        if dpg.does_item_exist("btn_sweep_run"):
            dpg.configure_item("btn_sweep_run", enabled=True)
        if dpg.does_item_exist("btn_sweep_cancel"):
            dpg.configure_item("btn_sweep_cancel", enabled=False)
        if dpg.does_item_exist("btn_sweep_export"):
            dpg.configure_item("btn_sweep_export", enabled=True)

    def _sweep_engine_log(self, msg: str):
        """Called by sweep engine for log messages."""
        self.log(f"[Sweep] {msg}")

    # --- Torque Telemetry ---
    def calculate_current_force(self):
        """Calculates theoretical total force output at current time."""
        if not self.sequencer.is_playing: return 0.0, False
        
        total_force = 0.0
        cur_t = self.sequencer.current_time
        
        for t in self.sequencer.tracks:
            for clip in t.clips:
                 if clip.start_time <= cur_t < (clip.start_time + clip.duration):
                     # Calculate instantaneous amplitude
                     # Relative time in clip
                     rel_t = cur_t - clip.start_time
                     amp = self._wave_amplitude(clip, rel_t)
                     total_force += amp
        
        # Apply Global Gain (from UI if available, else 1.0)
        gain = 1.0
        if dpg.does_item_exist("slider_gain"):
             gain = dpg.get_value("slider_gain") / 100.0
        
        normalized = (total_force * gain / 32767.0) * 100.0
        
        # Check if we are "active" (i.e. inside any clip)
        is_active = False
        for t in self.sequencer.tracks:
            for clip in t.clips:
                 if clip.start_time <= cur_t < (clip.start_time + clip.duration):
                     is_active = True
                     break
            if is_active: break

        return max(-100.0, min(100.0, normalized)), is_active

    def _log_frequency_output(self, requested_freq_hz: float):
        info = engine.describe_frequency_output(requested_freq_hz)
        key = (info["backend"], info["requested_hz"], info["actual_hz"])
        if key != self._last_logged_freq_output:
            self.log(
                f"Freq ({info['backend']}): requested {info['requested_hz']:.2f} Hz, actual {info['actual_hz']:.2f} Hz"
            )
            self._last_logged_freq_output = key

    # --- Transport Logic ---
    def toggle_play(self):
        self.sequencer.is_playing = not self.sequencer.is_playing
        if self.sequencer.is_playing:
            dpg.configure_item("btn_play", label="Stop")
            self.sequencer.last_tick = time.time()
            self.sweep_markers = []
            
            # Reset Stats (from Main)
            self.stats_peak = 0.0
            self.stats_min = 9999.0
            self.stats_sum = 0.0
            self.stats_count = 0
            if dpg.does_item_exist("txt_peak"): dpg.set_value("txt_peak", "Peak: --")
            if dpg.does_item_exist("txt_avg"): dpg.set_value("txt_avg", "Avg: --")
            if dpg.does_item_exist("txt_min"): dpg.set_value("txt_min", "Min: --")
            
            # Auto-rewind if at end
            # Auto-rewind if at end AND we have content
            max_dur = 0
            has_content = False
            for t in self.sequencer.tracks:
                for c in t.clips:
                    max_dur = max(max_dur, c.start_time + c.duration)
                    has_content = True
            
            if has_content and self.sequencer.current_time >= max_dur - 0.1:
                self.sequencer.current_time = 0.0
        else:
            dpg.configure_item("btn_play", label="Play")
            engine.stop_effect() # Stop all
            self.log_api("stop_effect", {"scope": "all"})
            self._last_logged_freq_output = None
            if dpg.does_item_exist("status_fallback"):
                dpg.configure_item("status_fallback", show=False)
            
            # Reset valid states (HEAD)
            for k in self.track_states:
                self.track_states[k] = {
                    'effect_id': -1,
                    'clip_id': None,
                    'clip_type': None,
                    'effect_start_time': None,
                    'phase_acc': 0.0,
                    'last_sweep_update_local': None,
                    'last_sent_freq': None,
                    'last_mag': None,
                    'last_freq': None,
                    'creation_pending': False,
                }
            
            # Reset active IDs (Main)
            for t in self.sequencer.tracks:
                for c in t.clips: c.active_effect_id = -1

    def action_restart(self):
        self.sequencer.current_time = 0.0
        self.sequencer.last_tick = time.time()
        # Reset effect states
        engine.stop_effect()
        self.log_api("stop_effect", {"scope": "all"})
        self._last_logged_freq_output = None
        if dpg.does_item_exist("status_fallback"):
            dpg.configure_item("status_fallback", show=False)
        for k in self.track_states:
            self.track_states[k] = {
                'effect_id': -1,
                'clip_id': None,
                'clip_type': None,
                'effect_start_time': None,
                'phase_acc': 0.0,
                'last_sweep_update_local': None,
                'last_sent_freq': None,
                'last_mag': None,
                'last_freq': None,
                'creation_pending': False,
            }
        for t in self.sequencer.tracks:
            for c in t.clips: c.active_effect_id = -1
        self.sweep_markers = []
        self.log("Restarted")

    def get_canvas_relative_pos(self, global_mouse_pos):
        # Helper to get coords relative to timeline content
        try:
             # Rely on node_pos vs scroll.
             c_min = dpg.get_item_rect_min("timeline_canvas")
             return global_mouse_pos[0] - c_min[0], global_mouse_pos[1] - c_min[1]
        except:
             return 0,0

    def _is_mouse_in_timeline_viewport(self, global_mouse_pos):
        timeline_hit = self._mouse_hits_timeline(global_mouse_pos)
        blocking_hover, hovered_tags = self._highlight_and_report()
        self._refresh_mouse_status(global_mouse_pos, timeline_hit, hovered_tags, blocking_hover)

        if not timeline_hit:
            return False

        if blocking_hover:
            return False

        return True

    def _safe_get_rect(self, tag):
        try:
            return dpg.get_item_rect_min(tag), dpg.get_item_rect_size(tag)
        except Exception:
            return None, None

    def _point_in_rect(self, pt, rect_min, rect_size):
        if not rect_min or not rect_size:
            return False
        max_x = rect_min[0] + rect_size[0]
        max_y = rect_min[1] + rect_size[1]
        return rect_min[0] <= pt[0] <= max_x and rect_min[1] <= pt[1] <= max_y

    def _mouse_hits_timeline(self, global_mouse_pos):
        if dpg.is_item_hovered("panel_inspector"):
            return False
        if self._is_any_inspector_hovered():
            return False

        # Explicitly block if the pointer is inside the inspector/log columns even when not hovering items
        insp_min, insp_size = self._safe_get_rect("panel_inspector")
        log_min, log_size = self._safe_get_rect("panel_log")
        if self._point_in_rect(global_mouse_pos, insp_min, insp_size):
            return False
        if self._point_in_rect(global_mouse_pos, log_min, log_size):
            return False

        if not dpg.does_item_exist("timeline_scroll"):
            return False

        # Primary check: rely on DearPyGui hover for scroll or canvas
        if dpg.is_item_hovered("timeline_scroll") or dpg.is_item_hovered("timeline_canvas"):
            return True

        # Fallback: geometry check on scroll rect
        scroll_min, scroll_size = self._safe_get_rect("timeline_scroll")
        return self._point_in_rect(global_mouse_pos, scroll_min, scroll_size)

    def _on_global_mouse_move(self, sender, app_data):
        global_pos = dpg.get_mouse_pos(local=False)
        timeline_hit = self._mouse_hits_timeline(global_pos)
        blocking_hover, hovered_tags = self._highlight_and_report()
        self._refresh_mouse_status(global_pos, timeline_hit, hovered_tags, blocking_hover)

    def _highlight_and_report(self):
        hovered_blocking = False
        hovered_tags = []
        for tag, cfg in self._hover_configs.items():
            if not dpg.does_item_exist(tag):
                continue
            is_hover = dpg.is_item_hovered(tag)
            self._apply_hover_highlight(tag, cfg, is_hover)
            if is_hover:
                hovered_tags.append(tag)
                if tag in self._blocking_hover_tags:
                    hovered_blocking = True
        inspector_hover_labels = self._get_hovered_inspector_labels()
        if inspector_hover_labels:
            hovered_blocking = True
            hovered_tags.extend(inspector_hover_labels)
        return hovered_blocking, hovered_tags

    def _get_hovered_inspector_labels(self):
        labels = []
        for inspector in self.inspectors:
            tag = inspector.tag_tab
            if not dpg.does_item_exist(tag):
                continue
            if dpg.is_item_hovered(tag):
                target_clip = inspector.clip or self.sequencer.selected_clip
                if target_clip:
                    label = target_clip.name or f"Clip {target_clip.type}"
                else:
                    label = "Live"
                labels.append(f"inspector:{label}")
        return labels

    def _is_any_inspector_hovered(self):
        return bool(self._get_hovered_inspector_labels())

    def _refresh_mouse_status(self, global_mouse_pos, timeline_hit, hovered_tags, blocking_hover):
        if not dpg.does_item_exist(self._mouse_status_tag):
            return
        tags = ",".join([t for t in hovered_tags if t])
        status = f"Mouse: {int(global_mouse_pos[0])},{int(global_mouse_pos[1])} | Timeline={timeline_hit} | Blocking={blocking_hover} | Hovering={tags}"
        try:
            dpg.set_value(self._mouse_status_tag, status)
        except Exception:
            pass

    def _set_mouse_status_visibility(self, visible: bool):
        self._mouse_status_visible = visible
        if dpg.does_item_exist(self._mouse_status_tag):
            dpg.configure_item(self._mouse_status_tag, show=visible)
        if dpg.does_item_exist("menu_mouse_status"):
            dpg.set_value("menu_mouse_status", visible)

    def _on_mouse_status_checkbox(self, sender, app_data):
        self._set_mouse_status_visibility(bool(app_data))

    def _on_wheel_graph_checkbox(self, sender, app_data):
        self._wheel_graph_visible = bool(app_data)
        if dpg.does_item_exist("menu_wheel_graph"):
            dpg.set_value("menu_wheel_graph", self._wheel_graph_visible)
        if hasattr(self, 'sequencer'):
            self.render_timeline()

    def _on_force_graph_checkbox(self, sender, app_data):
        self._force_graph_visible = bool(app_data)
        if dpg.does_item_exist("menu_force_graph"):
            dpg.set_value("menu_force_graph", self._force_graph_visible)
        if hasattr(self, 'sequencer'):
            self.render_timeline()

    def _on_poll_rate_changed(self, sender, app_data):
        """Handle manual poll rate override from UI."""
        rate = int(app_data)
        # Check if this differs from the auto-detected 75% rate
        auto_rate = int(engine.detected_poll_rate_hz * 0.75)
        is_manual = abs(rate - auto_rate) > 5  # 5Hz tolerance for rounding
        
        if is_manual:
            engine.set_target_update_rate(rate, is_manual_override=True)
        else:
            engine.clear_manual_poll_rate_override()
            engine.set_target_update_rate(rate, is_manual_override=False)
        
        if is_manual:
            self.log(f"Manual poll rate override: {rate}Hz")
        else:
            self.log(f"Poll rate: {rate}Hz (auto)")

    def _set_backend_mode(self, mode: str):
        mode = (mode or "").strip().lower()
        if mode != "directinput":
            self.log("Only DirectInput backend is enabled in this build")
            return
        if self.backend_mode == mode:
            return
        self.backend_mode = mode
        engine.set_directinput_hwnd(self._get_viewport_hwnd())
        engine.set_backend_mode(mode)

        if dpg.does_item_exist("menu_backend_di"):
            dpg.set_value("menu_backend_di", mode == "directinput")

        # Reconnect to apply backend change
        if dpg.does_item_exist("device_combo"):
            val = dpg.get_value("device_combo")
            if val and "No Devices" not in val:
                self.connect_device_by_name(val)

        self.log(f"Backend set to {mode.upper()}")

    def _get_viewport_hwnd(self):
        try:
            if hasattr(dpg, "get_viewport_platform_handle"):
                hwnd = dpg.get_viewport_platform_handle()
                if hwnd:
                    return int(hwnd)
        except Exception:
            pass
        try:
            import win32gui
            return win32gui.FindWindow(None, "MotorBuster")
        except Exception:
            return None

    def _apply_hover_highlight(self, tag, cfg, highlighted):
        if not dpg.does_item_exist(tag):
            return
        colors = cfg.get("hover") if highlighted else cfg.get("default")
        if not colors:
            colors = {}
        bg_color = colors.get("bg")
        border_color = colors.get("border")
        try:
            kwargs = {}
            if bg_color is not None:
                kwargs["bg_color"] = bg_color
            if border_color is not None:
                kwargs["border_color"] = border_color
            if kwargs:
                dpg.configure_item(tag, **kwargs)
        except Exception:
            pass

    def _configure_hover_defaults(self):
        for tag in self._hover_configs.keys():
            cfg = self._hover_configs.get(tag)
            if cfg and dpg.does_item_exist(tag):
                self._apply_hover_highlight(tag, cfg, False)

    def _track_index_from_rel_y(self, rel_y: float) -> int:
        """Map canvas-relative Y into track index; ignore the wheel graph band."""
        track_h = 80
        height_offset = self._active_wheel_graph_height()
        if rel_y < height_offset:
            return -1
        return int((rel_y - height_offset) // track_h)

    def _active_wheel_graph_height(self) -> float:
        return self.wheel_graph_height if self._wheel_graph_visible else 0

    def get_clip_at_pos(self, rel_x, rel_y):
        track_idx = self._track_index_from_rel_y(rel_y)
        if track_idx < 0:
            return None
        click_time = max(0.0, rel_x / self.sequencer.zoom_x)
        
        if 0 <= track_idx < len(self.sequencer.tracks):
             for clip in self.sequencer.tracks[track_idx].clips:
                 if clip.start_time <= click_time < (clip.start_time + clip.duration):
                     return clip
        return None

    def _get_resize_hover(self, track_idx, rel_x):
        """Check for the closest clip edge within the resize threshold and prefer the selected clip."""
        if 0 <= track_idx < len(self.sequencer.tracks):
             track = self.sequencer.tracks[track_idx]
             threshold = self.resize_threshold_px
             best_clip = None
             best_edge = None
             best_score = (threshold, 1)
             selected_clip = self.sequencer.selected_clip
             zoom = self.sequencer.zoom_x
             for clip in track.clips:
                 start_px = clip.start_time * zoom
                 end_px = (clip.start_time + clip.duration) * zoom
                 for edge_pos, edge_name in ((start_px, "left"), (end_px, "right")):
                     delta = abs(rel_x - edge_pos)
                     if delta < threshold:
                         score = (delta, 0 if clip is selected_clip else 1)
                         if score < best_score:
                             best_score = score
                             best_clip = clip
                             best_edge = edge_name
             if best_clip:
                 return best_clip, best_edge
        return None, None

    def _get_clips_in_rect(self, rect_min, rect_max):
        if rect_min is None or rect_max is None:
            return []
        x_min, y_min = rect_min
        x_max, y_max = rect_max
        selected = []
        track_height = 80
        height_offset = self._active_wheel_graph_height()
        for idx, track in enumerate(self.sequencer.tracks):
            clip_top = height_offset + idx * track_height + 20
            clip_bottom = height_offset + (idx + 1) * track_height - 5
            if clip_bottom < y_min or clip_top > y_max:
                continue
            for clip in track.clips:
                clip_start_px = clip.start_time * self.sequencer.zoom_x
                clip_end_px = clip_start_px + clip.duration * self.sequencer.zoom_x
                if clip_end_px < x_min or clip_start_px > x_max:
                    continue
                selected.append(clip)
        return selected

    def _update_multi_selection_rect(self):
        if not self.multi_select_box_start or not self.multi_select_box_end:
            return
        x0, y0 = self.multi_select_box_start
        x1, y1 = self.multi_select_box_end
        rect_min = (min(x0, x1), min(y0, y1))
        rect_max = (max(x0, x1), max(y0, y1))
        self.multi_selected_clips = self._get_clips_in_rect(rect_min, rect_max)

    def _is_ctrl_down(self) -> bool:
        return dpg.is_key_down(dpg.mvKey_LControl) or dpg.is_key_down(dpg.mvKey_RControl)

    def _toggle_clip_highlight(self, clip: Clip):
        clip_id = clip.id
        clip_label = clip.name or clip.type
        if clip_id in self.highlighted_clip_ids:
            self.highlighted_clip_ids.remove(clip_id)
            self.log(f"Unhighlighted {clip_label}")
        else:
            self.highlighted_clip_ids.add(clip_id)
            self.log(f"Highlighted {clip_label}")

    def _set_clip_highlight(self, clip: Clip):
        clip_id = clip.id
        if clip_id not in self.highlighted_clip_ids or len(self.highlighted_clip_ids) > 1:
            self.highlighted_clip_ids.clear()
            self.highlighted_clip_ids.add(clip_id)
            self.log(f"Highlighted {clip.name or clip.type}")

    def update_loop(self):
        # Track Target for Drop/Drag
        mpos = dpg.get_mouse_pos(local=False)
        rel_x, rel_y = self.get_canvas_relative_pos(mpos)
        mouse_in_timeline = self._is_mouse_in_timeline_viewport(mpos)
        track_idx = self._track_index_from_rel_y(rel_y)
        self.drag_target_track_idx = track_idx if mouse_in_timeline else -1
        interactive_timeline = mouse_in_timeline or getattr(self.sequencer, 'is_scrubbing', False)
        ctrl_down = self._is_ctrl_down()

        # KEYBOARD SHORTCUTS
        if dpg.is_key_pressed(dpg.mvKey_Delete):
            if dpg.is_item_hovered("timeline_canvas") or dpg.is_item_focused("timeline_scroll") or ctrl_down:
                if not self.delete_multi_selected_clips():
                    self.delete_selected_clip()

        if dpg.is_key_down(dpg.mvKey_LControl) or dpg.is_key_down(dpg.mvKey_RControl):
            if dpg.is_key_pressed(dpg.mvKey_C):
                if self.sequencer.selected_clip:
                    self.sequencer.clipboard = self.sequencer.selected_clip.to_dict()
                    self.log(f"Copied {self.sequencer.selected_clip.name or 'Clip'}")

            if dpg.is_key_pressed(dpg.mvKey_V):
                if self.sequencer.clipboard:
                    data = self.sequencer.clipboard
                    new_clip = Clip.from_dict(data)
                    new_clip.id = str(uuid.uuid4())
                    new_clip.start_time = self.sequencer.current_time

                    target_t_idx = new_clip.track_index
                    if 0 <= target_t_idx < len(self.sequencer.tracks):
                        t = self.sequencer.tracks[target_t_idx]
                        t.clips.append(new_clip)
                        actual_start = self._avoid_overlap_on_drag(t, new_clip, new_clip.start_time)
                        new_clip.start_time = actual_start

                        self.sequencer.selected_clip = new_clip
                        self.update_inspector_ui()
                        self.log("Pasted Clip")

        # DOUBLE CLICK CHECK (Open Inspector)
        if mouse_in_timeline and dpg.is_mouse_button_double_clicked(dpg.mvMouseButton_Left):
            m_pos = dpg.get_mouse_pos(local=False)
            rx, ry = self.get_canvas_relative_pos(m_pos)
            d_clip = self.get_clip_at_pos(rx, ry)
            if d_clip:
                self.sequencer.selected_clip = d_clip
                self.create_floating_inspector(d_clip)

        # Mouse logic for Dragging Clips vs Scrubbing vs Resizing
        track_h = 80
        mouse_left_down = dpg.is_mouse_button_down(dpg.mvMouseButton_Left)
        mouse_left_just_pressed = mouse_left_down and not self.mouse_left_button_down
        self.mouse_left_button_down = mouse_left_down
        if mouse_left_just_pressed and not ctrl_down and mouse_in_timeline:
            self.highlighted_clip_ids.clear()
            self.multi_selected_clips = []

        resize_clip_hover, resize_edge = (None, None)
        if interactive_timeline and not self.sequencer.drag_clip and not self.sequencer.resize_clip and not getattr(self.sequencer, 'is_scrubbing', False):
            resize_clip_hover, resize_edge = self._get_resize_hover(track_idx, rel_x)

        if not interactive_timeline:
            # Cancel interactions when outside the timeline while keeping the rest of the loop running.
            if self.sequencer.drag_clip:
                self.sequencer.drag_clip = None
            if self.sequencer.resize_clip:
                self.sequencer.resize_clip = None
            if hasattr(self.sequencer, 'is_scrubbing'):
                self.sequencer.is_scrubbing = False
            self.sequencer.drag_time_offset = 0.0
            self._highlight_drag_last_clip_id = None
            self._set_system_cursor_visible(True)
            if self.multi_select_box_active:
                self.multi_select_box_active = False
                self.multi_select_box_start = None
                self.multi_select_box_end = None
        elif mouse_left_down:
            if mouse_left_just_pressed and ctrl_down and mouse_in_timeline:
                self.multi_select_box_active = True
                self.multi_select_box_start = (rel_x, rel_y)
                self.multi_select_box_end = (rel_x, rel_y)
                self.multi_selected_clips = []
            if self.multi_select_box_active:
                self.multi_select_box_end = (rel_x, rel_y)
                self._update_multi_selection_rect()
            else:
                if mouse_left_just_pressed and resize_clip_hover:
                    clip, edge = resize_clip_hover, resize_edge
                    self.sequencer.selected_clip = clip
                    self.sequencer.resize_clip = clip
                    self.sequencer.resize_edge = edge
                    self.sequencer.resize_initial_dur = clip.duration
                    self.sequencer.resize_initial_time = clip.start_time
                    self.update_inspector_ui()
                    return

                if self.sequencer.resize_clip:
                    clip = self.sequencer.resize_clip
                    track = self.sequencer.tracks[clip.track_index]
                    cur_mouse_t = rel_x / self.sequencer.zoom_x

                    if self.sequencer.resize_edge == "right":
                        desired_dur = max(0.1, cur_mouse_t - clip.start_time)
                        clip.duration = self._limit_right_resize(track, clip, desired_dur)
                    elif self.sequencer.resize_edge == "left":
                        old_end = self.sequencer.resize_initial_time + self.sequencer.resize_initial_dur
                        desired_start = min(old_end - 0.1, max(0.0, cur_mouse_t))
                        new_start = self._limit_left_resize(track, clip, desired_start, old_end)
                        clip.start_time = new_start
                        clip.duration = old_end - new_start

                    self.update_inspector_ui()

                elif self.sequencer.drag_clip:
                    pointer_time = rel_x / max(1.0, self.sequencer.zoom_x)
                    new_t = max(0.0, pointer_time + self.sequencer.drag_time_offset)

                    new_track_idx = self._track_index_from_rel_y(rel_y)

                    clip = self.sequencer.drag_clip
                    clip.start_time = new_t

                    if 0 <= new_track_idx < len(self.sequencer.tracks):
                        if clip.track_index != new_track_idx:
                            old_track = self.sequencer.tracks[clip.track_index]
                            if clip in old_track.clips:
                                old_track.clips.remove(clip)
                            clip.track_index = new_track_idx
                            self.sequencer.tracks[new_track_idx].clips.append(clip)

                        active_track = self.sequencer.tracks[clip.track_index]
                        snapped_start = self._snap_to_edges(active_track, clip, clip.start_time)
                        clip.start_time = self._avoid_overlap_on_drag(active_track, clip, snapped_start)

                    self.update_inspector_ui()

                elif interactive_timeline:
                    hover_clip = None
                    if not self.sequencer.is_scrubbing:
                        hover_clip = self.get_clip_at_pos(rel_x, rel_y)

                    if mouse_left_just_pressed:
                        if hover_clip and not self.sequencer.is_scrubbing:
                            if ctrl_down:
                                self._highlight_drag_last_clip_id = hover_clip.id
                                self._toggle_clip_highlight(hover_clip)
                            else:
                                self._set_clip_highlight(hover_clip)
                                self.sequencer.selected_clip = hover_clip
                                self.update_inspector_ui()

                                clip_px_start = hover_clip.start_time * self.sequencer.zoom_x
                                clip_px_end = (hover_clip.start_time + hover_clip.duration) * self.sequencer.zoom_x
                                edge_threshold = self.resize_threshold_px

                                if abs(rel_x - clip_px_start) < edge_threshold:
                                    self.sequencer.resize_clip = hover_clip
                                    self.sequencer.resize_edge = "left"
                                    self.sequencer.resize_initial_dur = hover_clip.duration
                                    self.sequencer.resize_initial_time = hover_clip.start_time
                                elif abs(rel_x - clip_px_end) < edge_threshold:
                                    self.sequencer.resize_clip = hover_clip
                                    self.sequencer.resize_edge = "right"
                                    self.sequencer.resize_initial_dur = hover_clip.duration
                                    self.sequencer.resize_initial_time = hover_clip.start_time
                                else:
                                    self.sequencer.drag_clip = hover_clip
                                    mouse_time = rel_x / max(1.0, self.sequencer.zoom_x)
                                    self.sequencer.drag_time_offset = hover_clip.start_time - mouse_time
                        elif not ctrl_down:
                            self.highlighted_clip_ids.clear()
                            self.multi_selected_clips = []
                            was_scrubbing = self.sequencer.is_scrubbing
                            self.sequencer.is_scrubbing = True
                            new_t = max(0.0, rel_x / self.sequencer.zoom_x)
                            if rel_x < 15:
                                new_t = 0.0
                            self.sequencer.current_time = new_t
                            # Only trigger haptics if position changed
                            if not hasattr(self, '_last_scrub_position') or abs(new_t - self._last_scrub_position) > 1e-6:
                                self._last_scrub_position = new_t
                                self._trigger_scrub_haptics(new_t)
                            if not was_scrubbing:
                                self.sequencer.selected_clip = None
                                self.update_inspector_ui()
                    elif ctrl_down and hover_clip:
                        if hover_clip.id != self._highlight_drag_last_clip_id:
                            self._highlight_drag_last_clip_id = hover_clip.id
                            self._toggle_clip_highlight(hover_clip)
                    elif self.sequencer.is_scrubbing:
                        new_t = max(0.0, rel_x / self.sequencer.zoom_x)
                        if rel_x < 15:
                            new_t = 0.0
                        self.sequencer.current_time = new_t
                        # Only trigger haptics if position changed
                        if not hasattr(self, '_last_scrub_position') or abs(new_t - self._last_scrub_position) > 1e-6:
                            self._last_scrub_position = new_t
                            self._trigger_scrub_haptics(new_t)
        else:
            # Mouse Up - Release Drag/Resize/Scrub
            if self.sequencer.drag_clip:
                self.sequencer.drag_clip = None
            if self.sequencer.resize_clip:
                self.sequencer.resize_clip = None
            if hasattr(self.sequencer, 'is_scrubbing'):
                self.sequencer.is_scrubbing = False
            self.sequencer.drag_time_offset = 0.0
            self._highlight_drag_last_clip_id = None
            # Reset scrub position tracking
            if hasattr(self, '_last_scrub_position'):
                delattr(self, '_last_scrub_position')
            if self.multi_select_box_active:
                self.multi_select_box_active = False
                self.multi_select_box_start = None
                self.multi_select_box_end = None
        
        if self.sequencer.is_playing:
            now = time.time()
            dt = now - self.sequencer.last_tick
            self.sequencer.last_tick = now
            self.sequencer.current_time += dt
            
            # Auto-Stop / Loop Logic
            max_end = 0.0
            has_clips = False
            for t in self.sequencer.tracks:
                for c in t.clips:
                    max_end = max(max_end, c.start_time + c.duration)
                    has_clips = True
            
            if has_clips:
                if dpg.get_value("chk_loop"):
                    # Loop Mode: restart immediately at end
                    if self.sequencer.current_time >= max_end:
                        self.sequencer.current_time = 0.0
                        self.sequencer.last_tick = now
                        engine.stop_effect()
                        self.log_api("stop_effect", {"scope": "all"})
                        for k in self.track_states:
                            self.track_states[k] = {
                                'effect_id': -1,
                                'clip_id': None,
                                'clip_type': None,
                                'chain_end_time': None, # Reset chain info
                                'chain_end_time': None, # Reset chain info
                                'effect_start_time': None,
                                'hw_start_time': None,  # NEW: Time when RunHapticEffect was called
                                'phase_acc': 0.0,
                                'last_sweep_update_local': None,
                                'last_sent_freq': None,
                                'last_mag': None,
                                'last_freq': None,
                            }
                        for t in self.sequencer.tracks:
                            for c in t.clips:
                                c.active_effect_id = -1
                        self.sweep_markers = []
                else:
                    # Normal Mode: Stop exactly at end
                    if self.sequencer.current_time >= max_end:
                        self.sequencer.current_time = max_end
                        self.toggle_play() # Force Stop

            if self.sequencer.is_playing: self.process_sequencer_logic()
            
            # --- Diagnostic Freeze Test ---
            # Freeze cycle: 1s breathe, 2s pause, repeat
            if self.sequencer.is_playing and self.freeze_test_active:
                now_freeze = time.time()
                if self._should_trigger_freeze_now:
                    self._should_trigger_freeze_now = False
                    self._freeze_cycle_phase = "breathe"
                    self._freeze_cycle_phase_end = now_freeze + 1.0
                elif now_freeze >= self._freeze_cycle_phase_end:
                    if self._freeze_cycle_phase == "breathe":
                        self._freeze_cycle_phase = "pause"
                        self._freeze_cycle_phase_end = now_freeze + 2.0
                    else:
                        self._freeze_cycle_phase = "breathe"
                        self._freeze_cycle_phase_end = now_freeze + 1.0

                if self._freeze_cycle_phase == "pause":
                    remaining = max(0.0, self._freeze_cycle_phase_end - now_freeze)
                    if remaining > 0:
                        self.log("Freeze Test: Pausing 2s...")
                        time.sleep(remaining)
            
            # Update Telemetry
            force, is_active = self.calculate_current_force()
            dpg.set_value("force_gauge", force)

            # Auto-Scroll Playhead Tracking
            if dpg.does_item_exist("chk_track_playhead") and dpg.get_value("chk_track_playhead"):
                 if self.sequencer.is_playing:
                     playhead_px = self.sequencer.current_time * self.sequencer.zoom_x
                     try:
                        # Use rect size for actual visible width (get_item_width returns config val like -1)
                        viewport_w = dpg.get_item_rect_size("timeline_scroll")[0]
                     except:
                        viewport_w = 400 # Fallback
                        
                     # Center it
                     target_x = playhead_px - (viewport_w / 2)
                     dpg.set_x_scroll("timeline_scroll", max(0, target_x))
            
        dpg.set_value("time_display", f"{self.sequencer.current_time:.2f}s")
        # Timeline render is handled in main UI loop now

        
        # Draw Cursor Overlay AFTER timeline render (z-order)
        should_hide_system = False
        if resize_clip_hover or self.sequencer.resize_clip:
             should_hide_system = True
             self._draw_resize_cursor(rel_x, rel_y)
        
        self._set_system_cursor_visible(not should_hide_system)

        # Manual command response timeout (Cmd tab)
        if getattr(self, '_cmd_expects_response', False):
            if self._cmd_timeout < time.time() - self._cmd_sent_time:
                self._cmd_expects_response = False
                if dpg.does_item_exist('txt_cmd_status'):
                    dpg.set_value('txt_cmd_status', 'No response (timeout)')



        try:
            if dpg.does_item_exist("win_about") and dpg.is_item_shown("win_about"):
                if dpg.does_item_exist("txt_github") and dpg.is_item_hovered("txt_github"):
                    ctypes.windll.user32.SetCursor(self.cursor_hand)
        except Exception:
            pass

    def _record_wheel_sample(self, current_time: float):
        if self.sequencer.drag_clip or self.sequencer.resize_clip or getattr(self.sequencer, 'is_scrubbing', False):
            return
        raw = engine.get_axis_value(-1)
        if raw is None:
            return
        axis_scale = self.wheel_axis_scale if self.wheel_axis_scale else 32767.0
        norm = raw / axis_scale
        norm = max(-1.0, min(1.0, norm))

        # Always update live value for the dot
        self._last_live_wheel_norm = norm

        # Only record history if playing
        if not self.sequencer.is_playing:
            return

        if self.wheel_history:
            last_t = self.wheel_history[-1]["time"]
            # Detect loop or rewind -> Clear history to maintain sorted order for bisect
            if current_time < last_t:
                 self.wheel_history.clear()

        self.wheel_history.append({"time": current_time, "value": norm})
        if self.wheel_history_limit and len(self.wheel_history) > self.wheel_history_limit:
            excess = len(self.wheel_history) - self.wheel_history_limit
            if excess > 0:
                del self.wheel_history[:excess]
        self.hide_playhead_until_next_sample = False

    def _record_force_sample(self, current_time: float):
        if self.sequencer.drag_clip or self.sequencer.resize_clip or getattr(self.sequencer, 'is_scrubbing', False):
            return
            
        start_t = time.perf_counter()
        force_pct, _ = self.calculate_current_force()
        norm = force_pct / 100.0
        norm = max(-1.0, min(1.0, norm))

        if not self.sequencer.is_playing:
            return

        if self.force_history:
            last_t = self.force_history[-1]["time"]
            if current_time < last_t:
                 self.force_history.clear()

        self.force_history.append({"time": current_time, "value": norm})
        
        limit = self.wheel_history_limit if self.wheel_history_limit else 10000 
        if len(self.force_history) > limit:
            excess = len(self.force_history) - limit
            if excess > 0:
                del self.force_history[:excess]

    def _clear_wheel_history(self):
        self.wheel_history.clear()
        self.hide_playhead_until_next_sample = False

    def _draw_wheel_graph(self, total_w: float, scroll_x: Optional[float] = None, scroll_w: float = 0):
        if not self._wheel_graph_visible:
            return
        height = max(0, self.wheel_graph_height)
        if height <= 0:
            return

        gain = max(0.1, self.wheel_graph_gain)

        dpg.draw_rectangle([0, 0], [total_w, height], color=(40, 40, 50, 220), fill=(20, 20, 25, 190), parent="timeline_canvas")

        center_y = height / 2
        
        # Optimize Grid Lines: Only draw visible grid lines? 
        # For now, just simplistic drawing, but we could cull these too. 
        # The main bottleneck is the massive point history.
        if total_w > 0:
            dpg.draw_line([0, center_y], [total_w, center_y], color=(100, 110, 140, 200), thickness=1, parent="timeline_canvas")
        
        # Grid Dots - simplified or culling could be applied here too
        # But let's focus on history points first.
        
        y_scale = 1.0 # Default scale
        
        if self.wheel_history:
            segments = []
            current_segment = []
            prev_time = None
            gap_threshold = 0.25  # seconds between samples considered a jump
            
            # Calculate Visible Time Range
            # Margin of error (e.g. 2 second extra) so we don't clip lines entering the screen
            margin_s = 2.0 
            
            # If scroll info is missing (None) or invalid, draw everything (safe default)
            if scroll_x is None or scroll_w <= 0:
                start_time = 0.0
                end_time = float('inf')
            else:
                start_time = max(0.0, (scroll_x / self.sequencer.zoom_x) - margin_s)
                end_time = ((scroll_x + scroll_w) / self.sequencer.zoom_x) + margin_s

            # Bisect to find start index
            # We need a key for bisect, but we have list of dicts.
            # Construct ad-hoc or use a custom search. Bisect key available in Python 3.10
            # Assuming Python 3.10+
            start_idx = 0
            if start_time > 0:
                 # Custom bisect for list of dicts if key not supported or just to be safe/compatible
                 # bisect.bisect_left(self.wheel_history, start_time, key=lambda x: x["time"]) 
                 # Manual binary search is safer if uncertain about version
                 lo, hi = 0, len(self.wheel_history)
                 while lo < hi:
                     mid = (lo + hi) // 2
                     if self.wheel_history[mid]["time"] < start_time:
                         lo = mid + 1
                     else:
                         hi = mid
                 start_idx = lo
                 # Backtrack slightly to ensure we connect from off-screen
                 if start_idx > 0: start_idx -= 1
            
            # -------------------------------------------------------------------------
            # PASS 1: Calculate Scale (if enabled)
            # -------------------------------------------------------------------------
            if self.wheel_graph_auto_scale:
                 local_max_abs = 0.0
                 # Scan visible range
                 # Reuse start_idx computed above
                 for i in range(start_idx, len(self.wheel_history)):
                     s = self.wheel_history[i]
                     if s["time"] > end_time: break
                     val = abs(s["value"])
                     if val > local_max_abs: local_max_abs = val
                 
                 # Avoid div/0 or huge scaling on noise
                 # Minimum full-scale reference: 0.05 (5% of max torque)
                 # If signal is smaller than 5%, we just stick to 5% scale to avoid looking at noise.
                 safe_max = max(0.05, local_max_abs)
                 y_scale = 1.0 / safe_max
            
            # -------------------------------------------------------------------------
            # PASS 2: Adaptive Downsampling + Draw Segments
            # -------------------------------------------------------------------------
            # Calculate LOD based on pixel density
            # Assuming data at ~1000Hz, zoom_x is pixels per second
            if self.sequencer.zoom_x > 0:
                pixels_per_sample = self.sequencer.zoom_x / 1000.0
            else:
                pixels_per_sample = 1.0  # Fallback
            
            # Determine decimation factor and strategy
            use_minmax = False
            if pixels_per_sample >= 0.5:
                # High zoom: Full detail (render every point or every other point)
                decimation = 1 if pixels_per_sample >= 1.0 else 2
            elif pixels_per_sample >= 0.1:
                # Medium zoom: Simple decimation
                decimation = max(2, int(0.5 / pixels_per_sample))
            else:
                # Low zoom: Min-max downsampling to preserve envelope
                decimation = max(10, int(0.5 / pixels_per_sample))
                use_minmax = True
            
            # Build downsampled points list
            downsampled_points = []
            i = start_idx
            
            while i < len(self.wheel_history):
                sample = self.wheel_history[i]
                sample_time = sample["time"]
                
                if sample_time > end_time:
                    break
                
                if use_minmax and i + decimation < len(self.wheel_history):
                    # Min-Max downsampling: Find min and max in this bucket
                    bucket_end = min(i + decimation, len(self.wheel_history))
                    min_val = float('inf')
                    max_val = float('-inf')
                    min_time = sample_time
                    max_time = sample_time
                    
                    for j in range(i, bucket_end):
                        if self.wheel_history[j]["time"] > end_time:
                            break
                        val = self.wheel_history[j]["value"]
                        if val < min_val:
                            min_val = val
                            min_time = self.wheel_history[j]["time"]
                        if val > max_val:
                            max_val = val
                            max_time = self.wheel_history[j]["time"]
                    
                    # Add both min and max points in time order
                    if min_time <= max_time:
                        downsampled_points.append({"time": min_time, "value": min_val})
                        if min_val != max_val:  # Only add if different
                            downsampled_points.append({"time": max_time, "value": max_val})
                    else:
                        downsampled_points.append({"time": max_time, "value": max_val})
                        downsampled_points.append({"time": min_time, "value": min_val})
                    
                    i += decimation
                else:
                    # Simple decimation or full detail
                    downsampled_points.append(sample)
                    i += decimation
            
            # Now render the downsampled points
            for i, sample in enumerate(downsampled_points):
                sample_time = sample["time"]
                
                x = sample_time * self.sequencer.zoom_x
                if x < 0: 
                    continue
                    
                # Apply Gain AND Auto-Scale
                if self.wheel_graph_auto_scale:
                    val = sample["value"] * y_scale
                else:
                    val = sample["value"] * gain
                    
                # Clamp for drawing bounds
                val = max(-1.0, min(1.0, val))
                
                y = (height / 2) - val * (height / 2 - 6)

                if prev_time is not None:
                    dt = sample_time - prev_time
                    if dt < 0 or dt > gap_threshold:
                        if len(current_segment) >= 2:
                            segments.append(current_segment)
                        current_segment = []

                current_segment.append([x, y])
                prev_time = sample_time

            if len(current_segment) >= 2:
                segments.append(current_segment)

            for segment in segments:
                dpg.draw_polyline(segment, color=(100, 220, 255, 220), thickness=2, parent="timeline_canvas")

        if self._should_show_wheel_playhead():
            playhead_x = self.sequencer.current_time * self.sequencer.zoom_x
            dpg.draw_line([playhead_x, 0], [playhead_x, height], color=(255, 60, 60), thickness=1, parent="timeline_canvas")

            # Use live value if available, else fallback to history
            raw_val = self._last_live_wheel_norm if hasattr(self, '_last_live_wheel_norm') else 0.0
            
            # Same scaling for playhead
            if self.wheel_graph_auto_scale:
                 # For playhead, we should use the same scale as the graph for consistency, 
                 # but the playhead technically shows "current" value which might be outside the computed visual range 
                 # (e.g. if we are scrolling history). 
                 # Uses the computed y_scale from the render pass.
                 disp_val = raw_val * y_scale
            else:
                 disp_val = raw_val * gain
            
            disp_val = max(-1.0, min(1.0, disp_val))
            y = (height / 2) - disp_val * (height / 2 - 6)
            
            dpg.draw_circle([playhead_x, y], 4, color=(255, 150, 60), fill=(255, 150, 60), parent="timeline_canvas")
            dpg.draw_text([4, 4], f"Wheel: {raw_val*100:.1f}%", size=12, color=(220, 220, 255), parent="timeline_canvas")

    def _on_wheel_graph_auto_scale(self, sender, app_data):
        self.wheel_graph_auto_scale = bool(app_data)
        if hasattr(self, 'sequencer'):
            self.render_timeline()


    def _should_show_wheel_playhead(self) -> bool:
        return not self.hide_playhead_until_next_sample


    def _throttle_when_idle(self):
        if self.sequencer.is_playing:
            return
        if self.sequencer.drag_clip or self.sequencer.resize_clip or getattr(self.sequencer, 'is_scrubbing', False):
            return
        time.sleep(0.010)

    def _trigger_scrub_haptics(self, time_s: float):
        """Reset playback state when scrubbing so normal sequencer logic handles it correctly."""
        # Only trigger haptics if actually playing
        if not self.sequencer.is_playing:
            return
        
        # Simple throttle: Don't spam resets
        now = time.time()
        if hasattr(self, '_last_scrub_haptic_time'):
            if now - self._last_scrub_haptic_time < 0.05: # 50ms throttle for state reset
                return
        self._last_scrub_haptic_time = now

        # PROPER FIX: Reset all track states so process_sequencer_logic treats 
        # the new position as a fresh start. This prevents conflicts between
        # scrub-initiated effects and normal playback transitions.
        
        # Stop all current effects
        engine.stop_effect()
        self.log_api("stop_effect", {"scope": "all", "reason": "scrub"})
        
        # Reset all track states - this forces process_sequencer_logic to 
        # re-evaluate and start fresh from the new playhead position
        for track_idx in list(self.track_states.keys()):
            self.track_states[track_idx] = {
                'effect_id': -1,
                'clip_id': None,
                'clip_type': None,
                'chain_end_time': None,
                'effect_start_time': None,
                'hw_start_time': None,
                'phase_acc': 0.0,
                'last_sweep_update_local': None,
                'last_sent_freq': None,
                'last_mag': None,
                'last_freq': None,
                'clip_was_infinite': False,
                'creation_pending': False,
            }
        
        # Clear clip active effect IDs
        for track in self.sequencer.tracks:
            for clip in track.clips:
                clip.active_effect_id = -1
        
        # Let process_sequencer_logic handle starting effects at the new position
        # on the next frame - it will use start_new_effect with proper state management

    def process_sequencer_logic(self):
        cur_t = self.sequencer.current_time
        periodic_clip_types = ("Sine", "Square", "Triangle", "Sawtooth", "SawtoothUp", "SawtoothDown")
        
        # Read Global Gain
        global_gain = 1.0
        if dpg.does_item_exist("slider_gain"):
             global_gain = dpg.get_value("slider_gain") / 100.0

        # Iterate Tracks (not clips) to manage monophonic channel state
        for t_idx, track in enumerate(self.sequencer.tracks):
            if t_idx not in self.track_states:
                self.track_states[t_idx] = {
                    'effect_id': -1,
                    'clip_id': None,
                    'clip_type': None,
                    'clip_type': None,
                    'chain_end_time': None,
                    'effect_start_time': None,
                    'hw_start_time': None,
                    'last_sweep_update_local': None,
                    'last_sent_freq': None,
                    'last_mag': None,
                    'last_freq': None,
                    'clip_was_infinite': False,
                    'creation_pending': False,
                }
            
            state = self.track_states[t_idx]
            prev_cid = state['clip_id']
            eff_id = state['effect_id']
            prev_ctype = state['clip_type']
            
            # Find current logical clip
            current_clip = self.sequencer.get_clip_at_precise(t_idx, cur_t)
            curr_cid = current_clip.id if current_clip else None
            
            # Helper to update active effect with new clip
            def start_new_effect(start_phase=-1):
                if not current_clip:
                    return -1
                clip_start = current_clip.start_time
                
                # CHAIN DURATION LOGIC
                # If this clip is part of a contiguous chain, we want the effect to run 
                # until the end of the CHAIN, not just this clip.
                chain_end = self._get_contiguous_chain_end(track, current_clip)
                state['chain_end_time'] = chain_end
                
                playhead = max(cur_t, clip_start)
                # Calculate remaining time against CHAIN end
                remaining_ms = int(max(0.0, chain_end - playhead) * 1000)
                dur_ms = remaining_ms + self.UPDATE_LENGTH_BUFFER_MS
                eff_mag = int(current_clip.magnitude * global_gain)
                state['clip_was_infinite'] = self._clip_is_infinite(current_clip)

                # Build descriptor for engine.play_descriptor
                type_map = {
                    "Sine": "periodic_sine",
                    "Square": "square",
                    "Triangle": "triangle",
                    "SawtoothUp": "sawtoothup",
                    "SawtoothDown": "sawtoothdown",
                    "Sawtooth": "sawtoothup",
                    "Constant": "constant",
                    "Spring": "spring",
                    "Damper": "damper",
                    "Inertia": "inertia",
                    "Friction": "friction",
                }
                tkey = type_map.get(current_clip.type, "periodic_sine")

                direction_mode = (current_clip.direction_mode or "Polar").lower()
                direction = {}
                if direction_mode == "cartesian":
                    direction = {"x": current_clip.x, "y": current_clip.y, "z": current_clip.z}
                elif direction_mode == "spherical":
                    direction = {"yaw": max(0, min(36000, current_clip.yaw)), "pitch": max(0, min(36000, current_clip.pitch)), "distance": max(0, current_clip.distance)}
                else:
                    dir_deg = max(0, min(359, getattr(current_clip, "angle", 90)))
                    direction = {"angle": int(dir_deg * 100), "radius": max(0, current_clip.radius)}

                # Phase: slider stores 0-359 deg; API expects 0-35900 (hundredths). 36000 wraps to 0.
                if start_phase != -1:
                    phase_payload = max(0, min(35900, int(start_phase)))
                else:
                    phase_deg = max(0, min(359, int(getattr(current_clip, "start_phase", 0))))
                    phase_payload = phase_deg * 100

                desc = {
                    "type": tkey,
                    "frequency_hz": float(current_clip.frequency),
                    "magnitude": eff_mag,
                    "phase": phase_payload,
                    "direction_mode": direction_mode,
                    "direction": direction,
                    "start_mag": current_clip.start_mag,
                    "end_mag": current_clip.end_mag,
                    "envelope": {
                        "attack_length": current_clip.attack_length,
                        "fade_length": current_clip.fade_length,
                        "attack_level": 0,
                        "fade_level": 0,
                    },
                }

                if self.backend_mode == "directinput":
                    desc["length_us"] = int(max(0, dur_ms)) * 1000
                    desc["magnitude_unit"] = "s16"
                    desc["magnitude_src_s16"] = eff_mag
                    if tkey in ("periodic_sine", "square", "triangle", "sawtoothup", "sawtoothdown"):
                        safe_freq = max(0.1, float(current_clip.frequency))
                        desc["period_us"] = int(1_000_000.0 / safe_freq)
                    if tkey in ("spring", "damper", "inertia", "friction"):
                        coeff = abs(int(eff_mag))
                        coeff_di = self._s16_to_di(coeff)
                        desc["offset"] = 0
                        desc["positive_coefficient"] = -coeff
                        desc["negative_coefficient"] = coeff
                        desc["positive_saturation"] = coeff_di
                        desc["negative_saturation"] = coeff_di
                        desc["deadband"] = 0
                else:
                    desc["length_ms"] = dur_ms

                # Log actual effect type: use explicit diagnostic naming
                actual_type = tkey
                log_desc = {**desc, "type": actual_type}
                self.log_api("play_descriptor", log_desc)

                if current_clip.type in periodic_clip_types:
                    self._log_frequency_output(float(current_clip.frequency))
                
                # ASYNC: Queue the effect start instead of executing synchronously
                engine.queue_effect_start(desc, t_idx, current_clip.id, -1)
                
                # --- Built-in Freeze Test Trigger ---
                # Trigger freeze for both HW and SW modes if diagnostic test is active
                if self.freeze_test_active:
                     self._should_trigger_freeze_now = True
                # Note: We don't get the effect_id back immediately anymore since it's queued
                # The effect will be created when the event is processed
                new_id = -1  # Placeholder - actual ID will be set by event handler
                
                state['effect_start_time'] = playhead
                state['hw_start_time'] = playhead # Store HW start time
                state['last_sweep_update_local'] = None
                # Initialize tracking so the first continuation tick doesn't immediately trigger an update
                state['last_mag'] = current_clip.magnitude
                state['last_freq'] = current_clip.frequency
                state['last_sent_freq'] = current_clip.frequency
                state['last_start_mag'] = current_clip.start_mag
                state['last_end_mag'] = current_clip.end_mag
                state['creation_pending'] = True
                return new_id

            # State Machine
            if curr_cid == prev_cid:
                # CONTINUATION (Same clip still playing)
                
                # Check if we're waiting for async effect_id result
                if state.get('creation_pending'):
                    async_eff_id = engine.get_effect_id_result(t_idx, current_clip.id)
                    if async_eff_id is not None:
                        eff_id = async_eff_id
                        state['effect_id'] = eff_id
                        state['creation_pending'] = False
                        
                        if eff_id == -1:
                            reason = ""
                            try:
                                reason = engine.get_last_effect_error() or ""
                            except Exception:
                                reason = ""
                            if reason:
                                self.log(f"ERROR: HW Effect creation FAILED for Clip '{current_clip.name or current_clip.type}'. {reason}")
                            else:
                                self.log(f"ERROR: HW Effect creation FAILED for Clip '{current_clip.name or current_clip.type}'. No fallback.")
                            # Force reset state so we don't spam errors
                            state['effect_id'] = -1
                            state['creation_pending'] = False
                
                if current_clip and eff_id != -1:
                    # Update parameters if needed (Sweep)
                    remaining_ms = int((current_clip.duration - (cur_t - current_clip.start_time)) * 1000)
                    if remaining_ms < 0: remaining_ms = 0 # Safety
                    
                    if current_clip.type in ("Sine", "Square", "Triangle", "Sawtooth", "SawtoothUp", "SawtoothDown"):
                        # --- REAL-TIME UPDATE LOGIC ---
                        # Verify change against stored state or just update if sweep
                        is_sweep = current_clip.frequency != current_clip.frequency_end and current_clip.sweep_enabled
                        
                        # Check "Dirty" State (User changed sliders)
                        last_mag = state.get('last_mag', -1)
                        last_freq = state.get('last_freq', -1)
                        
                        # --- CHAIN VALIDATION ---
                        # Re-check chain length to support dynamic clip removal/resize
                        current_chain_end = self._get_contiguous_chain_end(track, current_clip)
                        stored_chain_end = state.get('chain_end_time')
                        chain_changed = (stored_chain_end is None) or (abs(current_chain_end - stored_chain_end) > 0.01)
                        
                        if chain_changed:
                             # Update stored state immediately so we don't spam updates if playhead paused?
                             # No, we want to spam the update command ONCE to fix the effect.
                             state['chain_end_time'] = current_chain_end

                        has_changed = (current_clip.magnitude != last_mag) or \
                                      (current_clip.frequency != last_freq and not is_sweep) or \
                                      chain_changed

                        # Target ~10 updates per Hz across the clip duration (time-based, not frame-based)
                        t_local = max(0.0, cur_t - (state.get('effect_start_time') if state.get('effect_start_time') is not None else current_clip.start_time))
                        sweep_ready = False
                        if is_sweep:
                            freq_span = abs(current_clip.frequency_end - current_clip.frequency)
                            steps = max(1, int(math.ceil(freq_span * 10.0)))
                            interval = current_clip.duration / steps if steps > 0 else current_clip.duration
                            last_local = state.get('last_sweep_update_local')
                            sweep_ready = ((last_local is None) and (t_local >= interval)) or (
                                last_local is not None and (t_local - last_local >= interval)
                            )

                        # Calculate current parameters for monitoring
                        progress = t_local / current_clip.duration
                        progress = max(0.0, min(1.0, progress))

                        if is_sweep:
                            current_freq = float(current_clip.frequency + (current_clip.frequency_end - current_clip.frequency) * progress)
                            current_freq = max(0.1, current_freq)
                        else:
                            current_freq = float(current_clip.frequency)

                        if current_clip.type in periodic_clip_types:
                            self._log_frequency_output(current_freq)

                        should_send = (is_sweep and sweep_ready) or has_changed

                        if should_send:
                             # Use Chain End for Duration, relative to Hardware Start
                             chain_end = state.get('chain_end_time', current_clip.start_time + current_clip.duration)
                             hw_start = state.get('hw_start_time', current_clip.start_time)
                             
                             # Total Duration from HW Start
                             remaining_ms = int(max(0.0, chain_end - hw_start) * 1000)
                             effect_len_ms = remaining_ms + self.UPDATE_LENGTH_BUFFER_MS
                             
                             # Phase stays fixed at clip's start phase (no recalculation during sweep)
                             start_deg = getattr(current_clip, "start_phase", 0)
                             phase_payload = start_deg * 100
                             
                             eff_mag = int(current_clip.magnitude * global_gain)
                             # Log all params being sent to engine
                             update_type = "update_effect_sine"
                             log_payload = {"effect_id": eff_id, "freq": current_freq, "mag": eff_mag, "phase": phase_payload}
                             length_us = None
                             if self.backend_mode == "directinput":
                                 eff_mag_di = self._s16_to_di(eff_mag)
                                 log_payload["mag"] = eff_mag_di
                                 log_payload["period_us"] = int(1_000_000.0 / max(0.1, float(current_freq)))
                                 length_us = int(max(0, effect_len_ms)) * 1000
                                 log_payload["length_us"] = length_us
                                 log_payload["magnitude_unit"] = "di"
                                 log_payload["magnitude_src_s16"] = eff_mag
                             else:
                                 log_payload["length_ms"] = effect_len_ms
                             self.log_api(update_type, log_payload)

                             new_eff_id = engine.update_effect_sine(
                                 eff_id,
                                 current_freq,
                                 log_payload["mag"] if self.backend_mode == "directinput" else eff_mag,
                                 effect_len_ms,
                                 phase=phase_payload,
                                 magnitude_unit=("di" if self.backend_mode == "directinput" else "s16"),
                                 duration_us=length_us,
                             )

                             if new_eff_id != -1:
                                 eff_id = new_eff_id
                                 state['effect_id'] = eff_id
                                 state['last_sent_freq'] = current_freq
                                 if is_sweep:
                                     state['last_sweep_update_local'] = t_local

                        # Update State
                        state['last_mag'] = current_clip.magnitude
                        state['last_freq'] = current_clip.frequency

                    elif current_clip.type == "Ramp":
                        # --- RAMP UPDATE LOGIC ---
                        current_chain_end = self._get_contiguous_chain_end(track, current_clip)
                        stored_chain_end = state.get('chain_end_time')
                        chain_changed = (stored_chain_end is None) or (abs(current_chain_end - stored_chain_end) > 0.01)

                        if chain_changed:
                            state['chain_end_time'] = current_chain_end

                        start_mag_raw = current_clip.start_mag
                        end_mag_raw = current_clip.end_mag
                        last_start = state.get('last_start_mag', None)
                        last_end = state.get('last_end_mag', None)

                        has_changed = (start_mag_raw != last_start) or (end_mag_raw != last_end) or chain_changed

                        if has_changed:
                            chain_end = state.get('chain_end_time', current_clip.start_time + current_clip.duration)
                            hw_start = state.get('hw_start_time', current_clip.start_time)
                            remaining_ms = int(max(0.0, chain_end - hw_start) * 1000)
                            effect_len_ms = remaining_ms + self.UPDATE_LENGTH_BUFFER_MS

                            start_mag = int(start_mag_raw * global_gain)
                            end_mag = int(end_mag_raw * global_gain)

                            log_payload = {"effect_id": eff_id, "start_mag": start_mag, "end_mag": end_mag}
                            length_us = None
                            if self.backend_mode == "directinput":
                                length_us = int(max(0, effect_len_ms)) * 1000
                                log_payload["length_us"] = length_us
                                log_payload["magnitude_unit"] = "s16"
                            else:
                                log_payload["length_ms"] = effect_len_ms

                            self.log_api("update_effect_ramp", log_payload)
                            new_eff_id = engine.update_effect_ramp(
                                eff_id,
                                start_mag,
                                end_mag,
                                effect_len_ms,
                            )
                            if new_eff_id != -1:
                                eff_id = new_eff_id
                                state['effect_id'] = eff_id

                        state['last_start_mag'] = start_mag_raw
                        state['last_end_mag'] = end_mag_raw

                # Update Phase Tracking for next frame's potential transition
                if current_clip and current_clip.type in ("Sine", "Square", "Triangle", "Sawtooth", "SawtoothUp", "SawtoothDown"):
                     t_local = max(0.0, cur_t - (state.get('effect_start_time') if state.get('effect_start_time') is not None else current_clip.start_time))
                     start_f = current_clip.frequency
                     end_f = current_clip.frequency_end if current_clip.sweep_enabled else start_f
                     k = (end_f - start_f) / max(1e-6, current_clip.duration)
                     norm_phase = (start_f * t_local + 0.5 * k * t_local * t_local) % 1.0
                     state['last_phase'] = int(norm_phase * 36000)
                     
                elif current_clip and eff_id == -1 and not state.get('creation_pending'):
                    # Recovery: Should be playing but isn't
                    eff_id = start_new_effect()
                    state['effect_id'] = eff_id
                    
            else:
                # TRANSITION (Clip Changed or Ended/Started)
                
                # Determine Start Phase for Gapless Continuity
                start_phase_override = -1
                if prev_cid is not None and current_clip and current_clip.type in ("Sine", "Square", "Triangle", "Sawtooth", "SawtoothUp", "SawtoothDown") and prev_ctype == current_clip.type:
                     # Robust calculation: Find previous clip object and calculate its end phase
                     prev_clip = self.sequencer.get_clip_by_id(prev_cid)
                     if prev_clip:
                         start_f_prev = prev_clip.frequency
                         end_f_prev = prev_clip.frequency_end if prev_clip.sweep_enabled else start_f_prev
                         k_prev = (end_f_prev - start_f_prev) / max(1e-6, prev_clip.duration)
                         t_end = prev_clip.duration
                         # Calc exact end phase
                         end_phase_val = (start_f_prev * t_end + 0.5 * k_prev * t_end * t_end) % 1.0
                         start_phase_override = int(end_phase_val * 36000)
                     elif 'last_phase' in state:
                         start_phase_override = state['last_phase']
                
                # Try Transfer (Reuse Effect)
                transferred = False
                should_stop_old = state.get('clip_was_infinite', False)
                if eff_id != -1 and current_clip and prev_ctype == current_clip.type and current_clip.type in ("Sine", "Square", "Triangle", "Sawtooth", "SawtoothUp", "SawtoothDown"):
                    # Reuse the sine effect via update
                    
                    # Update Chain End if we switched clips (which we did, since curr != prev)
                    # We need to re-evaluate the chain end from this new clip onwards
                    chain_end = self._get_contiguous_chain_end(track, current_clip)
                    state['chain_end_time'] = chain_end
                    
                    # Duration is now Time-to-Chain-End (Total from HW Start)
                    hw_start = state.get('hw_start_time', state.get('effect_start_time', 0))
                    
                    # Update parameters
                    dur_ms = int(max(0.0, chain_end - hw_start) * 1000) + self.UPDATE_LENGTH_BUFFER_MS
                    
                    eff_mag = int(current_clip.magnitude * global_gain)
                    
                    # Calculate Phase
                    start_deg = getattr(current_clip, "start_phase", 0)
                    phase_to_use = start_deg * 100 # Default user setting
                    
                    if start_phase_override != -1:
                         # Gapless Override takes precedence if we want continuity
                         # But wait, user wanted CONTROL.
                         # If user set a specific Phase, maybe we should use it?
                         # "I need you ... to set phase so it starts ... at same phase ... currently playing"
                         # This implies AUTO-CONTINUITY for SWEEP, but what about CLIP-TO-CLIP?
                         # "One ends at 0... Next starts at 0... I can choose the starting point"
                         # Implies manual setup. 
                         # So if start_phase is 0 (default), maybe use override?
                         # If start_phase is non-zero, use it?
                        if start_deg == 0:
                            phase_to_use = start_phase_override
                        else:
                            phase_to_use = start_deg * 100

                    # Clamp to valid API range (0..35900); 36000 wraps to 0
                    phase_to_use = max(0, min(35900, int(phase_to_use)))
                    
                    update_payload = {"effect_id": eff_id, "freq": current_clip.frequency, "mag": eff_mag, "phase": phase_to_use}
                    length_us = None
                    if self.backend_mode == "directinput":
                        eff_mag_di = self._s16_to_di(eff_mag)
                        update_payload["mag"] = eff_mag_di
                        update_payload["period_us"] = int(1_000_000.0 / max(0.1, float(current_clip.frequency)))
                        length_us = int(max(0, dur_ms)) * 1000
                        update_payload["length_us"] = length_us
                        update_payload["magnitude_unit"] = "di"
                        update_payload["magnitude_src_s16"] = eff_mag
                    else:
                        update_payload["length_ms"] = dur_ms
                    self.log_api("update_effect_sine", update_payload)
                    new_id = engine.update_effect_sine(
                        eff_id,
                        current_clip.frequency,
                        update_payload["mag"] if self.backend_mode == "directinput" else eff_mag,
                        dur_ms,
                        phase=phase_to_use,
                        magnitude_unit=("di" if self.backend_mode == "directinput" else "s16"),
                        duration_us=length_us,
                    )
                    if new_id != -1:
                        transferred = True
                        eff_id = new_id
                        state['effect_start_time'] = current_clip.start_time
                        state['creation_pending'] = False

                elif eff_id != -1 and current_clip and prev_ctype == current_clip.type and current_clip.type == "Ramp":
                    # Reuse the ramp effect via update
                    chain_end = self._get_contiguous_chain_end(track, current_clip)
                    state['chain_end_time'] = chain_end

                    hw_start = state.get('hw_start_time', state.get('effect_start_time', 0))
                    dur_ms = int(max(0.0, chain_end - hw_start) * 1000) + self.UPDATE_LENGTH_BUFFER_MS

                    start_mag_raw = current_clip.start_mag
                    end_mag_raw = current_clip.end_mag
                    start_mag = int(start_mag_raw * global_gain)
                    end_mag = int(end_mag_raw * global_gain)

                    update_payload = {"effect_id": eff_id, "start_mag": start_mag, "end_mag": end_mag}
                    if self.backend_mode == "directinput":
                        update_payload["length_us"] = int(max(0, dur_ms)) * 1000
                        update_payload["magnitude_unit"] = "s16"
                    else:
                        update_payload["length_ms"] = dur_ms

                    self.log_api("update_effect_ramp", update_payload)
                    new_id = engine.update_effect_ramp(
                        eff_id,
                        start_mag,
                        end_mag,
                        dur_ms,
                    )
                    if new_id != -1:
                        transferred = True
                        eff_id = new_id
                        state['effect_start_time'] = current_clip.start_time
                        state['creation_pending'] = False
                
                if not transferred:
                    # Stop Old
                    if eff_id != -1 and should_stop_old:
                        self.log_api("stop_effect", {"effect_id": eff_id})
                        engine.stop_effect(eff_id)
                        eff_id = -1
                    
                    # Start New
                    if current_clip:
                        eff_id = start_new_effect(start_phase=start_phase_override)
                
                # Save State
                state['effect_id'] = eff_id
                state['clip_id'] = curr_cid
                state['clip_type'] = current_clip.type if current_clip else None
                # Chain End Time is set in start_new_effect or Transition block
                state['effect_start_time'] = current_clip.start_time if current_clip else None
                # NOTE: Do NOT reset hw_start_time here if transferring, as we are reusing the HW effect!
                state['last_sweep_update_local'] = None
                if current_clip:
                    # Seed tracking to prevent an immediate "changed" update on the next tick
                    state['last_sent_freq'] = current_clip.frequency
                    state['last_mag'] = current_clip.magnitude
                    state['last_freq'] = current_clip.frequency
                    state['last_phase'] = start_phase_override if start_phase_override != -1 else 0
                    state['last_start_mag'] = current_clip.start_mag
                    state['last_end_mag'] = current_clip.end_mag
                    state['clip_was_infinite'] = self._clip_is_infinite(current_clip)
                else:
                    state['last_sent_freq'] = None
                    state['last_mag'] = None
                    state['last_freq'] = None
                    state['last_start_mag'] = None
                    state['last_end_mag'] = None
                    state['clip_was_infinite'] = False
        if not self.sequencer.is_playing:
            self._last_logged_freq_output = None


    # --- Rendering ---
    def render_grid(self, total_w, total_h):
        """Draws vertical grid lines and time labels based on current zoom."""
        target_px = 100.0
        ideal_dt = target_px / max(0.1, self.sequencer.zoom_x)
        auto_grid_time = self._nice_grid_interval(ideal_dt)
        grid_time = self.manual_timebase_value if (self.manual_timebase_override and self.manual_timebase_value) else auto_grid_time

        if dpg.does_item_exist("input_timebase"):
             if not self.manual_timebase_override and not dpg.is_item_active("input_timebase"):
                 dpg.set_value("input_timebase", grid_time)
                 self.manual_timebase_value = grid_time
             elif not self.manual_timebase_override:
                 self.manual_timebase_value = grid_time

        scroll_x = 0
        scroll_w = total_w
        pad_px = 200
        if dpg.does_item_exist("timeline_scroll"):
            try:
                scroll_x = dpg.get_x_scroll("timeline_scroll")
                rect = dpg.get_item_rect_size("timeline_scroll")
                if rect and rect[0] > 10: 
                    scroll_w = rect[0]
            except: pass

        start_px = max(0, scroll_x - pad_px)
        end_px = min(total_w, scroll_x + scroll_w + pad_px)
        t = math.floor((start_px / self.sequencer.zoom_x) / grid_time) * grid_time if grid_time > 0 else 0.0
        lines_drawn = 0
        max_lines = 600
        while True:
            x = t * self.sequencer.zoom_x
            if x > end_px or lines_drawn >= max_lines:
                break
            if x >= start_px:
                color = (60, 60, 60, 100)
                thickness = 1
                dpg.draw_line([x, 0], [x, total_h], color=color, thickness=thickness, parent="timeline_canvas")
                lines_drawn += 1
            t += grid_time

    def _nice_grid_interval(self, ideal_dt: float) -> float:
        if ideal_dt <= 0:
            return 0.01
        fine_threshold = 0.08
        fine_steps = (0.01, 0.02, 0.05)
        if ideal_dt <= fine_threshold:
            for step in reversed(fine_steps):
                if ideal_dt >= step:
                    return step
            return fine_steps[0]

        power = math.floor(math.log10(ideal_dt))
        base = 10 ** power
        frac = ideal_dt / base
        if frac < 1.5:
            nice = 1
        elif frac < 3:
            nice = 2
        elif frac < 7:
            nice = 5
        else:
            nice = 10
        return nice * base

    def render_timeline(self):
        if dpg.does_item_exist("timeline_canvas"):
            dpg.delete_item("timeline_canvas", children_only=True)

        active_graph_height = self._active_wheel_graph_height()
        y_offset = active_graph_height
        track_height = 80
        # Calculate dynamic total width based on content (clips + history)
        max_t = 60.0
        # Check clips
        for track in self.sequencer.tracks:
            for clip in track.clips:
                 end = clip.start_time + clip.duration
                 if end > max_t: max_t = end
        
        # Check history
        if self.wheel_history:
             last_hist = self.wheel_history[-1]["time"]
             if last_hist > max_t: max_t = last_hist
             
        # Add buffer
        max_t += 10.0
        
        total_w = max(3000, int(self.sequencer.zoom_x * max_t))
        total_h = int(active_graph_height + len(self.sequencer.tracks) * track_height)
        dpg.configure_item("timeline_canvas", width=total_w, height=total_h)
        
        # Apply deferred scroll (from zoom) now that canvas is resized
        if self._pending_scroll_x is not None:
            print(f"DEBUG RENDER: Applying Pending Scroll {self._pending_scroll_x:.1f} to Width {total_w}")
            dpg.set_x_scroll("timeline_scroll", self._pending_scroll_x)
            self._pending_scroll_x = None

        self.render_grid(total_w, total_h)
        
        if self._wheel_graph_visible and active_graph_height > 0:
            # Fetch scroll info for culling safely
            scroll_x = None
            scroll_w = 0
            try:
                if dpg.does_item_exist("timeline_scroll"):
                    scroll_x = dpg.get_x_scroll("timeline_scroll")
                    rect = dpg.get_item_rect_size("timeline_scroll")
                    if rect and rect[0] > 10:
                        scroll_w = rect[0]
            except: pass
            
            self._draw_wheel_graph(total_w, scroll_x, scroll_w)

        # Optimization: Calculate visible time range to cull off-screen clips
        visible_start_time = 0.0
        visible_end_time = float('inf')
        
        try:
             if dpg.does_item_exist("timeline_scroll"):
                 scroll_x = dpg.get_x_scroll("timeline_scroll")
                 # OPTIMIZATION FIX: get_item_width often returns 0/-1 for auto-sized items. 
                 # get_item_rect_size returns the actual rendered size on screen.
                 visible_width = dpg.get_item_rect_size("timeline_scroll")[0]
                 
                 # Map pixels to time (add buffer)
                 visible_start_time = max(0.0, (scroll_x - 300) / self.sequencer.zoom_x)
                 visible_end_time = (scroll_x + visible_width + 300) / self.sequencer.zoom_x
        except Exception:
            pass

        for i, track in enumerate(self.sequencer.tracks):
            is_target = (i == self.drag_target_track_idx)

            bg_col = (40, 40, 45, 50) if i % 2 == 0 else (35, 35, 40, 50)
            if i == self.drag_target_track_idx:
                bg_col = (60, 60, 80, 100)

            dpg.draw_rectangle([0, y_offset], [total_w, y_offset + track_height], color=bg_col, fill=bg_col, parent="timeline_canvas")
            dpg.draw_line([0, y_offset + track_height], [total_w, y_offset + track_height], color=(60, 60, 60), parent="timeline_canvas")
            dpg.draw_text([10, y_offset + 5], track.name, size=15, color=(200, 200, 200), parent="timeline_canvas")
            
            for clip in track.clips:
                # OPTIMIZATION: Visibility Check
                clip_end = clip.start_time + clip.duration
                if clip_end < visible_start_time or clip.start_time > visible_end_time:
                    continue

                x_start = clip.start_time * self.sequencer.zoom_x
                width = clip.duration * self.sequencer.zoom_x
                
                base_col = (100, 150, 255) if clip.type == "Sine" else (255, 100, 100)
                if clip.type == "Constant":
                    base_col = (100, 255, 100)
                if clip.type == "Ramp":
                    base_col = (255, 255, 100)
                if clip.type == "Sawtooth":
                    base_col = (255, 150, 50)

                is_highlighted = clip.id in self.highlighted_clip_ids
                is_multi_selected = clip in self.multi_selected_clips
                default_fill = (base_col[0], base_col[1], base_col[2], 150)
                highlight_border = (255, 220, 110, 220)
                highlight_fill = (255, 220, 110, 120)
                if is_highlighted or is_multi_selected:
                    border_col = highlight_border
                    fill_color = highlight_fill
                else:
                    border_col = (255, 255, 255) if clip == self.sequencer.selected_clip else base_col
                    fill_color = default_fill

                dpg.draw_rectangle([x_start, y_offset + 20], [x_start + width, y_offset + track_height - 5], color=border_col, thickness=2, fill=fill_color, parent="timeline_canvas")
                dpg.draw_text([x_start + 5, y_offset + 25], clip.name, size=13, parent="timeline_canvas")

                wave_points = self._clip_wave_points(
                    clip,
                    x_start + 4,
                    max(4.0, width - 8),
                    y_offset + 28,
                    y_offset + track_height - 12,
                    samples=max(20, int(width / 6))
                )
                dpg.draw_polyline(wave_points, color=(240, 240, 240, 220), thickness=2, parent="timeline_canvas")

            y_offset += track_height

        px = self.sequencer.current_time * self.sequencer.zoom_x
        dpg.draw_line([px, 0], [px, y_offset], color=(255, 50, 50), thickness=2, parent="timeline_canvas")

        if self.show_redlines and self.sweep_markers:
            for m in self.sweep_markers:
                mx = m.get("time", 0.0) * self.sequencer.zoom_x
                phase_txt = f"{m.get('phase', 0)/100.0:.1f}°"
                dpg.draw_line([mx, 0], [mx, y_offset], color=(200, 50, 50, 120), thickness=1, parent="timeline_canvas")
                dpg.draw_text([mx + 4, 8], phase_txt, size=12, color=(255, 120, 120), parent="timeline_canvas")

        if self.multi_select_box_active and self.multi_select_box_start and self.multi_select_box_end:
            x0 = min(self.multi_select_box_start[0], self.multi_select_box_end[0])
            y0 = min(self.multi_select_box_start[1], self.multi_select_box_end[1])
            x1 = max(self.multi_select_box_start[0], self.multi_select_box_end[0])
            y1 = max(self.multi_select_box_start[1], self.multi_select_box_end[1])
            dpg.draw_rectangle([x0, y0], [x1, y1], color=(130, 180, 255, 220), fill=(90, 140, 255, 90), thickness=1, parent="timeline_canvas")

    def _draw_resize_cursor(self, x, y):
        """Draws a custom double-headed arrow cursor at (x,y)."""
        # Draw on top of everything
        # Size - 30% smaller than 10 -> 7
        sz = 7 
        # Color
        col = (255, 255, 255, 255)
        outline_col = (0, 0, 0, 200) # Shadow/Outline
        
        # Thinner shaft for smaller size
        shaft_w = 3
        shaft_len = 4

        # Helper to draw arrow with outline
        def draw_arrow(cx, cy, direction):
            # direction: -1 left, 1 right
            offset = shaft_len * direction
            
            # Arrow Head
            # Tip
            p1 = [cx + (sz * direction) + offset, cy]
            # Back Top
            p2 = [cx + offset, cy - sz/1.5]
            # Back Bottom
            p3 = [cx + offset, cy + sz/1.5]
            
            # Shadow/Outline
            dpg.draw_triangle(p1, p2, p3, fill=outline_col, color=outline_col, parent="timeline_canvas", thickness=2)
            # Main White Triangle
            dpg.draw_triangle(p1, p2, p3, fill=col, color=col, parent="timeline_canvas")

        draw_arrow(x, y, -1) # Left
        draw_arrow(x, y, 1)  # Right
        
        # Center Line (Shaft)
        # Shadow
        dpg.draw_line([x - shaft_len, y], [x + shaft_len, y], color=outline_col, thickness=shaft_w+2, parent="timeline_canvas")
        # Main
        dpg.draw_line([x - shaft_len, y], [x + shaft_len, y], color=col, thickness=shaft_w, parent="timeline_canvas")

    def delete_selected_clip(self):
        clip = self.sequencer.selected_clip
        if not clip:
            return
        self.highlighted_clip_ids.discard(clip.id)
        self.sequencer.delete_clip(clip)
        self.sequencer.selected_clip = None
        self.update_inspector_ui()

    def delete_multi_selected_clips(self):
        if not self.multi_selected_clips:
            return False
        deleted = []
        for clip in list(self.multi_selected_clips):
            deleted.append(clip)
            self.highlighted_clip_ids.discard(clip.id)
            self.sequencer.delete_clip(clip)
        self.multi_selected_clips = []
        self.sequencer.selected_clip = None
        self.update_inspector_ui()
        self.log(f"Deleted {len(deleted)} clips")
        return True

    def on_drop_receive(self, sender, app_data):
        print(f"DEBUG DROP: Sender={sender}, Data={app_data}")
        self.handle_drop(app_data, sender) # Pass sender to debug

    def handle_drop(self, effect_type, sender="Unknown"):
        if not isinstance(effect_type, str): return
        
        mpos = dpg.get_mouse_pos(local=False)
        rel_x, rel_y = self.get_canvas_relative_pos(mpos)
        
        track_h = 80
        track_idx = self._track_index_from_rel_y(rel_y)
        
        self.log(f"Drop [{sender}]: {effect_type} at {int(rel_x)},{int(rel_y)} -> Tk {track_idx+1}")
        
        if 0 <= track_idx < len(self.sequencer.tracks):
             time_s = max(0.0, rel_x / self.sequencer.zoom_x)
             clip = self.sequencer.add_clip(track_idx, effect_type, time_s)
             self.sequencer.selected_clip = clip
             self.log(f"Created Clip: {time_s:.2f}s")
        else:
             self.log(f"Drop Skipped: Invalid Track {track_idx}")

    def canvas_click(self, sender, app_data):
        mouse_btn = app_data[0] # [0]=button, [1]=tag
        mpos = dpg.get_mouse_pos(local=False)
        if not self._is_mouse_in_timeline_viewport(mpos):
            return
        rel_x, rel_y = self.get_canvas_relative_pos(mpos)
        
        track_h = 80
        track_idx = self._track_index_from_rel_y(rel_y)
        time_s = rel_x / self.sequencer.zoom_x
        
        if mouse_btn == 1:
            # RIGHT CLICK: Context Menu
            # Check if clicked on a clip
            clip = self.get_clip_at_pos(rel_x, rel_y)
            
            if clip:
                # CLIP CONTEXT MENU
                if dpg.does_item_exist("win_clip_opts"): dpg.delete_item("win_clip_opts")
                self.sequencer.selected_clip = clip # Auto-select on right click
                self.renaming_clip = clip
                
                with dpg.window(tag="win_clip_opts", label="Clip Options", width=250, height=120, modal=True, show=True, pos=mpos):
                     dpg.add_text(f"Clip: {clip.type}")
                     dpg.add_input_text(tag="input_clip_name", default_value=clip.name, on_enter=True, callback=lambda: do_rename_clip(None, None, None))

                     def do_rename_clip(s, a, u):
                         name = dpg.get_value("input_clip_name")
                         if self.renaming_clip:
                             self.renaming_clip.name = name
                             self.update_inspector_ui()
                         dpg.delete_item("win_clip_opts")

                     def do_del_clip(s, a, u):
                         if self.renaming_clip:
                             self.sequencer.delete_clip(self.renaming_clip)
                             self.sequencer.selected_clip = None
                             self.update_inspector_ui()
                         dpg.delete_item("win_clip_opts")

                     with dpg.group(horizontal=True):
                         dpg.add_button(label="Rename", callback=do_rename_clip)
                         dpg.add_button(label="Delete", callback=do_del_clip)
                         dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item("win_clip_opts"))
            
            elif 0 <= track_idx < len(self.sequencer.tracks):
                # TRACK CONTEXT MENU
                self.renaming_track_idx = track_idx
                if dpg.does_item_exist("win_track_opts"): dpg.delete_item("win_track_opts")
                
                with dpg.window(tag="win_track_opts", label=f"Track {track_idx+1} Options", width=300, height=120, modal=True, show=True, pos=mpos):
                    dpg.add_input_text(tag="input_rename", default_value=self.sequencer.tracks[track_idx].name)
                    def do_rename(s, a, u):
                        name = dpg.get_value("input_rename")
                        if 0 <= self.renaming_track_idx < len(self.sequencer.tracks):
                            self.sequencer.tracks[self.renaming_track_idx].name = name
                        dpg.delete_item("win_track_opts")
                    def do_delete(s, a, u):
                        if 0 <= self.renaming_track_idx < len(self.sequencer.tracks):
                            del self.sequencer.tracks[self.renaming_track_idx]
                            self.sequencer.selected_clip = None 
                        dpg.delete_item("win_track_opts")
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Rename", callback=do_rename)
                        dpg.add_button(label="Delete Track", callback=do_delete)
                        dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item("win_track_opts"))
            return



    def update_inspector_ui(self):
        # Update all active inspector panels
        active = []
        for p in self.inspectors:
            if dpg.does_item_exist(p.tag_tab):
                p.update()
                active.append(p)
        self.inspectors = active

    def create_floating_inspector(self, clip=None, parent=None):
        # 1. Check if window already exists for this clip (only for floating windows)
        if clip and not parent:
            for p in self.inspectors:
                if p.clip == clip and p.is_window and dpg.does_item_exist(p.tag_tab):
                    dpg.configure_item(p.tag_tab, show=True)
                    dpg.focus_item(p.tag_tab)
                    # Bring to front? Focus usually does it.
                    return

        # 2. Cleanup closed inspectors
        self.inspectors = [p for p in self.inspectors if dpg.does_item_exist(p.tag_tab)]

        # 3. Create New
        pos = [400, 200]
        if not parent:
            # Cascade logic
            offset = len(self.inspectors) * 30
            pos = [400 + offset, 200 + offset]
            
        p = InspectorPanel(self, parent, clip, pos=pos)
        self.inspectors.append(p)
        if parent:
             dpg.set_value(parent, p.tag_tab)

    # --- Palette Drag Source ---
    def make_drag_source(self, label, type):
        with dpg.group():
             dpg.add_button(label=label, width=100)
             with dpg.drag_payload(drag_data=type): # Removed payload_type for compatibility
                 dpg.add_text(f"Effect: {label}")


    def on_mouse_wheel(self, sender, app_data):
        # app_data is value
        # Lock view to tracks: Only zoom if hovering timeline
        if dpg.is_item_hovered("timeline_canvas") or dpg.is_item_hovered("timeline_scroll"):
            self.manual_timebase_override = False
            self.manual_timebase_value = None
            
            # --- Zoom to Mouse Logic ---
            scale_factor = 1.15
            
            # Get Current State
            scroll_x = dpg.get_x_scroll("timeline_scroll")
            
            # Robust Calculation using Drawing Mouse Pos (Canvas Space)
            # This avoids unreliable global screen coordinate calculations
            canvas_mouse_x, _ = dpg.get_drawing_mouse_pos()
            
            # Canvas X is effectively (Scroll X + Viewport Relative X)
            content_x_px = canvas_mouse_x
            
            # Calculate Viewport Relative X for re-centering later
            viewport_rel_x = content_x_px - scroll_x
            
            # Current time at mouse cursor
            mouse_time = content_x_px / max(0.1, self.sequencer.zoom_x)
            
            # Apply Zoom
            new_zoom = self.sequencer.zoom_x
            if app_data > 0:
                new_zoom *= scale_factor
            elif app_data < 0:
                new_zoom /= scale_factor
            
            # Clamp settings
            new_zoom = max(10.0, min(50000.0, new_zoom))
            self.sequencer.zoom_x = new_zoom
            
            # Calculate new scroll position to match mouse_time at same screen position
            target_scroll_x = (mouse_time * new_zoom) - viewport_rel_x
            
            # Defer the scroll update until AFTER the timeline is resized in render_timeline
            # preventing the value from being clamped to the old width
            self._pending_scroll_x = max(0, target_scroll_x)
            print(f"DEBUG ZOOM: MouseT={mouse_time:.2f}, Zoom={new_zoom:.1f}, TargetScroll={target_scroll_x:.1f}, ViewportRel={viewport_rel_x}")

            # Prevent Vertical Scroll Drift
            total_h = self._active_wheel_graph_height() + len(self.sequencer.tracks) * 80
            win_h = dpg.get_item_height("timeline_scroll")
            if total_h <= win_h:
                dpg.set_y_scroll("timeline_scroll", 0)

    def on_timebase_change(self, sender, app_data):
        # User manually entered a timebase (e.g. 0.1)
        # We need to set zoom_x such that grid lines appear at this interval
        # Base target px ~ 100px?
        if app_data <= 0.00001: return
        target_px = 100.0
        self.sequencer.zoom_x = target_px / app_data
        self.manual_timebase_override = True
        self.manual_timebase_value = app_data
        

    def _set_system_cursor_visible(self, visible: bool):
        if not hasattr(self, 'cursor_visible'): self.cursor_visible = True
        
        # Only toggle if state changes to avoid flickering/counter issues
        if self.cursor_visible == visible: return
        
        self.cursor_visible = visible
        try:
            # Windows API ShowCursor
            # True = Increment display count, False = Decrement
            # We want to force it.
            
            if not visible:
                # Hide: Decrement until < 0
                while ctypes.windll.user32.ShowCursor(False) >= 0: pass
            else:
                # Show: Increment until >= 0
                while ctypes.windll.user32.ShowCursor(True) < 0: pass
                
        except Exception as e:
            print(f"Cursor Error: {e}")


    def load_fonts(self):
        with dpg.font_registry():
            # Try to load custom font if exists
            font_path = "assets/fonts/Inter-Regular.otf"
            font_bold_path = "assets/fonts/Inter-Bold.otf"
            
            # System Fallback (Segoe UI on Windows is standard and clean)
            sys_font_path = "C:/Windows/Fonts/segoeui.ttf"
            sys_bold_path = "C:/Windows/Fonts/segoeuib.ttf"
            
            used_path = None
            used_bold = None
            
            if os.path.exists(font_path):
                used_path = font_path
                used_bold = font_bold_path if os.path.exists(font_bold_path) else None
            elif os.path.exists(sys_font_path):
                used_path = sys_font_path
            # Add font chars for custom title bar icons: — (0x2014), □ (0x25A1), ✕ (0x2715)
            # We use a context manager or add_font_chars directly if supported, or remap.
            # DPG add_font_chars needs to be called on the font object.
            
            # Helper to add ranges
            def add_custom_chars(font_tag):
                dpg.add_font_range_hint(dpg.mvFontRangeHint_Default, parent=font_tag)
                # 0x2014 (Em Dash), 0x25A1 (Square), 0x00D7 (Mult Sign)
                dpg.add_font_chars([0x2014, 0x25A1, 0x00D7], parent=font_tag)

            if used_bold and os.path.exists(used_bold):
                try:
                    self.font_bold = dpg.add_font(used_bold, 20)
                    add_custom_chars(self.font_bold)
                    self.font_header = dpg.add_font(used_bold, 24)
                    add_custom_chars(self.font_header)
                    # Large icons font
                    self.font_large_icons = dpg.add_font(used_path if used_path else used_bold, 28) 
                    add_custom_chars(self.font_large_icons)
                except Exception as e:
                    self.log(f"Error loading bold/large font: {e}")
            
            if used_path and os.path.exists(used_path):
                try:
                    self.font_regular = dpg.add_font(used_path, 18)
                    add_custom_chars(self.font_regular)
                    if not hasattr(self, 'font_large_icons'): # Fallback if bold failed or didn't exist
                         self.font_large_icons = dpg.add_font(used_path, 28)
                         add_custom_chars(self.font_large_icons)

                    dpg.bind_font(self.font_regular)
                    print(f"Loaded Font: {used_path}")
                except Exception as e:
                     self.log(f"Error loading regular font: {e}")
            else:
                # Fallback to default
                dpg.bind_font(dpg.mvFont_Default)

    def apply_theme(self, mode="Dark Mode"):
        self.current_theme_mode = mode

        # Dark Mode — the only theme
        cols = {
             "grid_line": (60, 60, 60, 100),
             "track_bg_even": (40, 40, 45, 50),
             "track_bg_odd": (35, 35, 40, 50),
             "track_border": (60, 60, 60),
             "text_track": (200, 200, 200),
             "clip_sine": (100, 150, 255),
             "clip_const": (150, 255, 100),
             "clip_ramp": (255, 150, 100),
             "clip_saw": (255, 100, 100),
             "ruler_bg": (30, 30, 35),
             "ruler_border": (60, 60, 60),
             "ruler_line": (100, 100, 100),
             "ruler_tick": (150, 150, 150),
             "ruler_text": (200, 200, 200),
             "playhead": (255, 50, 50),
             "playhead_fill": (255, 100, 100),
             "text_green": (0, 255, 0),
         }
         
        ui = {
             "win_bg": (20, 20, 25),
             "text": (220, 220, 220),
             "btn": (45, 45, 50),
             "btn_hov": (60, 60, 65),
             "btn_act": (80, 80, 85),
             "frame_bg": (30, 30, 35),
             "frame_hov": (40, 40, 45),
             "menu_bg": (30, 30, 35),
             "popup_bg": (30, 30, 35),
             "border": (60, 60, 60),
             "check": (100, 150, 255),
             "link_color": (100, 200, 255),
         }


        self.theme_colors = cols
        
        with dpg.theme() as global_theme:
            with dpg.theme_component(dpg.mvAll):
                # Ensure borders are visible (Soft Light needs this specifically)
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1)
                
                # Core
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, ui["win_bg"])
                dpg.add_theme_color(dpg.mvThemeCol_Text, ui["text"])
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 8, 8)
                dpg.add_theme_color(dpg.mvThemeCol_Border, ui["border"])
                
                # Buttons
                dpg.add_theme_color(dpg.mvThemeCol_Button, ui["btn"])
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, ui["btn_hov"])
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, ui["btn_act"])
                
                # Frames (Inputs)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, ui["frame_bg"])
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, ui["frame_hov"])
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, ui["frame_hov"])
                
                # Menu / Popup
                dpg.add_theme_color(dpg.mvThemeCol_MenuBarBg, ui["menu_bg"])
                dpg.add_theme_color(dpg.mvThemeCol_PopupBg, ui["popup_bg"])
                
                # Child Windows (Panels)
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, ui["win_bg"])
                
                # Title Bars
                title_bg = ui["menu_bg"]
                dpg.add_theme_color(dpg.mvThemeCol_TitleBg, title_bg)
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, title_bg)
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgCollapsed, title_bg)
                
                # Table Borders
                dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight, ui["border"])
                dpg.add_theme_color(dpg.mvThemeCol_TableBorderStrong, ui["border"])
                
                # Misc
                dpg.add_theme_color(dpg.mvThemeCol_CheckMark, ui["check"])
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, ui["check"])
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, ui["check"])
                dpg.add_theme_color(dpg.mvThemeCol_Button, ui["btn"]) # Ensure buttons are correct
                
                # Scrollbars
                if "scrollbar_bg" in ui:
                     dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, ui["scrollbar_bg"])
                     dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, ui["scrollbar_grab"])
                     scrollbar_hov = ui.get("scrollbar_grab_hov", ui["scrollbar_grab"])
                     scrollbar_act = ui.get("scrollbar_grab_act", ui["check"])
                     dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabHovered, scrollbar_hov)
                     dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabActive, scrollbar_act)

                # Headers (Selectables, Menus, Collapsing Headers)
                if "header" in ui:
                    dpg.add_theme_color(dpg.mvThemeCol_Header, ui["header"])
                    dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, ui.get("header_hover", ui["header"]))
                    dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, ui.get("header_active", ui["header"]))

                # Tabs
                if "tab" in ui or "btn" in ui: 
                    # Fallback to btn if tab not defined, or specific tab color
                    base = ui.get("tab", ui.get("btn", (0,0,0)))
                    hov = ui.get("tab_hov", ui.get("btn_hov", (0,0,0)))
                    act = ui.get("tab_act", ui.get("btn_act", (0,0,0)))
                    
                    dpg.add_theme_color(dpg.mvThemeCol_Tab, base)
                    dpg.add_theme_color(dpg.mvThemeCol_TabHovered, hov)
                    dpg.add_theme_color(dpg.mvThemeCol_TabActive, act)
                    dpg.add_theme_color(dpg.mvThemeCol_TabUnfocused, ui.get("tab_act_unfocused", base))
                    dpg.add_theme_color(dpg.mvThemeCol_TabUnfocusedActive, ui.get("tab_act_unfocused", act))

        
        dpg.bind_theme(global_theme)

        with dpg.theme(tag="theme_title_bar_bg"):
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 0, 6, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 6, 6, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 6, category=dpg.mvThemeCat_Core)
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, ui["menu_bg"])

        if dpg.does_item_exist("title_bar"):
            dpg.bind_item_theme("title_bar", "theme_title_bar_bg")

        with dpg.theme(tag="theme_title_controls_box"):
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, ui["menu_bg"])
                dpg.add_theme_color(dpg.mvThemeCol_Border, ui["border"])
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 0, 0, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 3, 0, category=dpg.mvThemeCat_Core)
            with dpg.theme_component(dpg.mvWindowAppItem):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, ui["menu_bg"])
                dpg.add_theme_color(dpg.mvThemeCol_Border, ui["border"])
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 4, 4, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 3, 0, category=dpg.mvThemeCat_Core)

        if dpg.does_item_exist("title_controls_box"):
            dpg.bind_item_theme("title_controls_box", "theme_title_controls_box")
        
        # Trigger redraw if needed
        if hasattr(self, 'sequencer'):
            self.render_timeline()

        # Update specific items that don't auto-update with theme
        if dpg.does_item_exist("status_text"):
            # Only color it green if it's actually connected/green-candidate
            # Otherwise respect the Red disconnected state (or whatever current color is)
            curr_s = dpg.get_value("status_text")
            if "Connected" in curr_s:
                dpg.configure_item("status_text", color=self.theme_colors.get("text_green", (0, 255, 0)))
        if dpg.does_item_exist("txt_license_link"):
             # Use defined link color, fallback to Blue if missing
             link_col = ui.get("link_color", (100, 200, 255))
             dpg.configure_item("txt_license_link", color=link_col)


    def setup_ui(self):
        with dpg.theme() as global_theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (20, 20, 25))
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 0, 0)
        dpg.bind_theme(global_theme)
        
        # File Dialogs
        dpg.add_file_dialog(
            directory_selector=False, show=False, callback=self.action_save_file, tag="dlg_save",
            width=700, height=400, default_filename="project.motorbuster", label="Save Project As..."
        )
        dpg.add_file_extension(".motorbuster", color=(255, 255, 255, 255), parent="dlg_save")
        
        dpg.add_file_dialog(
            directory_selector=False, show=False, callback=self.action_load_file, tag="dlg_load",
            width=700, height=400, label="Open Project File..."
        )
        dpg.add_file_extension(".motorbuster", color=(255, 255, 255, 255), parent="dlg_load")
        dpg.add_file_extension(".*", parent="dlg_load")

        # Shortcuts
        def ctrl_pressed() -> bool:
            return dpg.is_key_down(dpg.mvKey_LControl) or dpg.is_key_down(dpg.mvKey_RControl)

        with dpg.handler_registry():
            dpg.add_key_press_handler(dpg.mvKey_S, callback=lambda: dpg.show_item("dlg_save") if ctrl_pressed() else None)
            dpg.add_key_press_handler(dpg.mvKey_O, callback=lambda: dpg.show_item("dlg_load") if ctrl_pressed() else None)

        with dpg.theme(tag="theme_no_padding"):
            with dpg.theme_component(dpg.mvAll):
                # WindowPadding y=0 to avoid extra gray area below the title bar
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 0, 0, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 4, 3, category=dpg.mvThemeCat_Core) # Restore default frame padding for buttons
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 4, category=dpg.mvThemeCat_Core)

        with dpg.theme(tag="theme_menu_popup"):
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 8, 6, category=dpg.mvThemeCat_Core)

        with dpg.theme(tag="theme_title_controls"):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 0, 0, 0))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (80, 80, 90, 80))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (120, 120, 140, 120))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (0, 0, 0, 0))
                dpg.add_theme_style(dpg.mvStyleVar_ButtonTextAlign, 0.5, 0.45, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 0, 1, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 6, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 0, category=dpg.mvThemeCat_Core)

        with dpg.theme(tag="theme_title_combo"):
            with dpg.theme_component(dpg.mvCombo):
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 3, 8, category=dpg.mvThemeCat_Core)

        with dpg.theme(tag="theme_inspector_panel"):
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 14, 14, category=dpg.mvThemeCat_Core)

        with dpg.window(tag="Main"):
            dpg.bind_item_theme("Main", "theme_no_padding")

            # Title Bar Section
            
            # Load Icon Texture (Existing code)
            icon_width, icon_height, icon_channels, icon_data = 0, 0, 0, []
            texture_loaded = False
            try:
                import os
                base_path = os.path.dirname(os.path.abspath(__file__))
                icon_png_path = os.path.join(base_path, "assets", "icon.png")
                width, height, channels, data = dpg.load_image(icon_png_path)
                
                with dpg.texture_registry(show=False):
                    dpg.add_static_texture(width=width, height=height, default_value=data, tag="icon_texture")
                texture_loaded = True
            except Exception as e:
                print(f"Failed to load icon texture: {e}")

            # Menu Bar (native title bar is provided by Windows)
            with dpg.menu_bar():
                with dpg.menu(label="File", tag="menu_file"):
                    dpg.add_menu_item(label="Save Project", shortcut="(Ctrl+S)", callback=lambda: dpg.show_item("dlg_save"))
                    dpg.add_menu_item(label="Open Project", shortcut="(Ctrl+O)", callback=lambda: dpg.show_item("dlg_load"))
                    dpg.add_separator()
                    dpg.add_menu_item(label="Exit", callback=lambda: dpg.stop_dearpygui())

                with dpg.menu(label="View", tag="menu_view"):
                    dpg.add_menu_item(label="Show Redlines", check=True, default_value=self.show_redlines, callback=self.toggle_redlines)
                    dpg.add_menu_item(label="Wheel Graph", check=True, default_value=self._wheel_graph_visible, callback=self._on_wheel_graph_checkbox)
                    dpg.add_menu_item(label="Mouse Status", check=True, tag="menu_mouse_status", default_value=self._mouse_status_visible, callback=self._on_mouse_status_checkbox)

                with dpg.menu(label="About", tag="menu_about"):
                    dpg.add_menu_item(label="About MotorBuster", callback=lambda: dpg.configure_item("win_about", show=True))

                dpg.bind_item_theme("menu_file", "theme_menu_popup")
                dpg.bind_item_theme("menu_view", "theme_menu_popup")
                dpg.bind_item_theme("menu_about", "theme_menu_popup")

                dpg.add_spacer(width=10)
                dpg.add_combo(tag="device_combo", width=220, callback=self.on_device_selected)
                dpg.add_button(label="Scan", callback=self.scan_devices)
                dpg.add_button(label="Connect", callback=self.connect_callback)
                dpg.add_text("Status: Disconnected", tag="status_text", color=(255, 100, 100))
                
                dpg.add_spacer(width=15)
                dpg.add_text("Rate:", color=(180, 180, 180))
                dpg.add_input_int(tag="poll_rate_input", width=50, default_value=500, min_value=10, max_value=1000, min_clamped=True, max_clamped=True, step=0, step_fast=0, callback=self._on_poll_rate_changed)
                dpg.add_text("Hz", color=(180, 180, 180))

            dpg.add_separator()
            # Spacing below title bar - Reduced to 0/minimal as requested
            # Content will sit flush against separator
            
            # About Dialog (v12 Content)
            with dpg.theme() as theme_about:
                with dpg.theme_component(dpg.mvAll):
                    dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 20, 20)
            
            with dpg.item_handler_registry(tag="handler_github_link"):
                dpg.add_item_clicked_handler(callback=lambda: webbrowser.open("https://github.com/"))
            
            # Centered position (assuming 1280x800 viewport: x=365, y=210)
            with dpg.window(tag="win_about", label="About MotorBuster", width=550, height=380, modal=True, show=False, pos=[365, 210], no_resize=True, no_collapse=True):
                dpg.add_text("MotorBuster (Version 1.0)")
                dpg.add_spacer(height=10)
                dpg.add_text("A modern force-feedback sequencer for motor and wheel testing.")
                dpg.add_spacer(height=10)
                dpg.add_text("Created by Swift & Napman")
                dpg.add_spacer(height=10)
                with dpg.group(horizontal=True, horizontal_spacing=0):
                    dpg.add_text("Source Code: Available on ")
                    dpg.add_text("GitHub", tag="txt_license_link", color=(100, 200, 255))
                    dpg.bind_item_handler_registry("txt_license_link", "handler_github_link")
                dpg.add_text("License: MIT License")
                dpg.add_spacer(height=10)
                dpg.add_text("This software is an independent project and is not affiliated with,\nendorsed by, or sponsored by Microsoft Corporation.", wrap=500)
                dpg.add_spacer(height=10)
                dpg.add_text("Copyright (c) 2025. Free and open source.")
                
                dpg.add_spacer(height=20)
                dpg.add_button(label="Close", width=100, callback=lambda: dpg.hide_item("win_about"))
                
            dpg.bind_item_theme("win_about", theme_about)

            # HW Capability Mode Modal
            with dpg.window(tag="win_ffb_probe", label="HW Capability Mode", width=600, height=520, modal=True, show=False, pos=[340, 150], no_resize=False):
                dpg.add_text("This mode will test your device's hardware FFB capabilities.")
                dpg.add_text("Please keep your hands ON the wheel/device but be prepared for movement.")
                dpg.add_spacer(height=10)

                dpg.add_checkbox(
                    label="Streamed updates (keepalive)",
                    tag="chk_probe_streamed",
                    callback=lambda s, a: self._on_probe_stream_mode("streamed"),
                )
                dpg.add_checkbox(
                    label="Non-streamed updates",
                    tag="chk_probe_nonstreamed",
                    callback=lambda s, a: self._on_probe_stream_mode("nonstreamed"),
                )
                
                dpg.add_text("Status: Idle", tag="txt_probe_status")
                dpg.add_progress_bar(tag="progress_probe", width=-1, default_value=0.0)
                
                dpg.add_spacer(height=10)
                with dpg.child_window(tag="child_probe_log", height=200, border=True):
                    dpg.add_text("Waiting to start...", tag="txt_probe_log", wrap=570)
                
                dpg.add_spacer(height=10)
                with dpg.group(tag="group_probe_feedback", show=False):
                    dpg.add_text("INTERACTIVE TEST:", color=(255, 200, 0))
                    dpg.add_text(tag="txt_probe_feedback_msg", wrap=570)
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Yes", width=120, callback=lambda: self._on_probe_feedback("YES"))
                        dpg.add_button(label="No", width=120, callback=lambda: self._on_probe_feedback("NO"))
                        dpg.add_button(label="Idk, try again", width=140, callback=lambda: self._on_probe_feedback("RETRY"))
                
                dpg.add_spacer(height=10)
                with dpg.group(horizontal=True, tag="group_probe_controls"):
                    dpg.add_button(label="Start test", tag="btn_probe_start", width=120, callback=self._exec_ffb_probe)
                    dpg.add_button(label="Cancel Test", width=120, callback=lambda: dpg.hide_item("win_ffb_probe"))
                
                with dpg.group(tag="group_probe_results", show=False):
                    dpg.add_separator()
                    dpg.add_text("RESULTS:", color=(100, 255, 100))
                    dpg.add_text(tag="txt_probe_recommendation", color=(255, 255, 255))
                    dpg.add_button(label="Close & Apply", width=150, callback=self._close_ffb_probe)



            
            # Set Main as a fallback drop target without payload type check
            # try: dpg.set_item_drop_callback("Main", self.on_drop_receive)
            # except: pass
            
            # Global Handlers
            with dpg.handler_registry():
                dpg.add_mouse_wheel_handler(callback=self.on_mouse_wheel)
                dpg.add_mouse_move_handler(callback=self._on_global_mouse_move)
                # dpg.add_key_press_handler(dpg.mvKey_Delete, callback=self.on_key_press)
                # dpg.add_key_press_handler(dpg.mvKey_Back, callback=self.on_key_press)

            # Top Bar
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=10)
                # Device controls moved to menu bar
                # dpg.add_text("MotorBuster", color=(100, 200, 255)) # Removed per request
                # dpg.add_text("| Rate: -- Hz", tag="status_fps", color=(150, 255, 150)) # REMOVED per user request
                
                dpg.add_spacer(width=20)
                # Force Monitor
                # Using a theme for the slider to look like a gauge/bar
                dpg.add_text("Force:")
                dpg.add_slider_float(tag="force_gauge", width=150, min_value=-100, max_value=100, format="%.0f%%")

                dpg.add_spacer(width=50)
                dpg.add_button(tag="btn_play", label="Play", width=80, callback=self.toggle_play)
                dpg.add_button(label="|<", tag="btn_restart", width=30, callback=self.action_restart)
                dpg.add_checkbox(label="Loop", tag="chk_loop")
                
                dpg.add_text("0.00s", tag="time_display")

                dpg.add_spacer(width=20)
                dpg.add_button(label="Clear", width=70, callback=lambda: self._clear_wheel_history())
                
                dpg.add_spacer(width=20)
                dpg.add_text("T:")
                dpg.add_input_float(tag="input_timebase", width=60, default_value=0.1, step=0, callback=self.on_timebase_change)
                dpg.add_spacer(width=15)
                dpg.add_checkbox(label="Track", tag="chk_track_playhead", default_value=False)

            dpg.add_separator()

        

        # --- SOLID TABLE LAYOUT ---
        with dpg.table(header_row=False, resizable=True, policy=dpg.mvTable_SizingStretchProp, 
                       borders_innerV=True, parent="Main"):
            dpg.add_table_column(width_fixed=True, init_width_or_weight=100) # Palette
            dpg.add_table_column() # Timeline
            dpg.add_table_column(width_fixed=True, init_width_or_weight=400) # Inspector & Log

            with dpg.table_row():
                
                # Col 1: Palette
                with dpg.child_window(height=-1):
                    
                    self.make_drag_source("Sine Wave", "Sine")
                    self.make_drag_source("Square", "Square")
                    self.make_drag_source("Triangle", "Triangle")
                    self.make_drag_source("Constant", "Constant")
                    self.make_drag_source("Saw Up", "SawtoothUp")
                    self.make_drag_source("Saw Down", "SawtoothDown")
                    self.make_drag_source("Spring", "Spring")
                    self.make_drag_source("Damper", "Damper")
                    self.make_drag_source("Inertia", "Inertia")
                    self.make_drag_source("Friction", "Friction")
                    dpg.add_spacer(height=20)
                    dpg.add_button(label="+ Add Track", width=100, callback=lambda: self.sequencer.tracks.append(Track(name="New Track")))

                # Col 2: Timeline
                with dpg.group(tag="timeline_group"):
                    # Scroll Window
                    with dpg.child_window(tag="timeline_scroll", horizontal_scrollbar=True, no_scroll_with_mouse=True):
                        with dpg.drawlist(width=3000, height=1000, tag="timeline_canvas"):
                            pass

                    dpg.add_text("Drop Effects Here", parent="timeline_scroll", color=(100,100,100))
                    dpg.add_text("", tag=self._mouse_status_tag, parent="timeline_scroll", color=(200, 210, 255), show=self._mouse_status_visible)

                    with dpg.item_handler_registry(tag="timeline_click_handler"):
                        dpg.add_item_clicked_handler(callback=self.canvas_click)

                    dpg.bind_item_handler_registry("timeline_canvas", "timeline_click_handler")

                    try:
                        dpg.set_item_drop_callback("timeline_scroll", self.on_drop_receive)
                    except Exception as e: print(f"Init Warning: {e}")

                # Col 3: Inspector (Top) & Log (Bottom)
                with dpg.group():
                    # Inspector Section (Top Half)
                    with dpg.child_window(tag="panel_inspector", height=450):
                        dpg.bind_item_theme("panel_inspector", "theme_inspector_panel")
                        # We use a Tab Bar here to host the single Live Inspector tab
                        # This keeps the look consistent and clean
                        with dpg.tab_bar(tag="inspector_tab_bar"):
                            # COM Port tab (first - alphabetical)
                            with dpg.tab(label="COM", tag="tab_com"):
                                dpg.add_spacer(height=4)
                                dpg.add_text("Serial Port", color=(200, 200, 255))
                                dpg.add_separator()
                                dpg.add_spacer(height=4)
                                with dpg.group(horizontal=True):
                                    dpg.add_combo(tag="serial_port_combo", width=220, callback=self._on_serial_port_selected)
                                    dpg.add_button(label="Scan", callback=self._scan_serial_ports)
                                dpg.add_spacer(height=3)
                                with dpg.group(horizontal=True):
                                    dpg.add_combo(tag="serial_baud_combo", width=100, default_value="115200", items=["9600","19200","38400","57600","115200","230400","460800","921600"], callback=self._on_serial_baud_changed)
                                    dpg.add_button(label="Connect", tag="btn_serial_connect", callback=self._connect_serial)
                                dpg.add_spacer(height=6)
                                dpg.add_text("COM: Offline", tag="txt_serial_status", color=(100, 100, 100))
                                dpg.add_spacer(height=5)
                                dpg.add_text("Bode Response:", color=(180, 180, 200))
                                dpg.add_text("Mag = --- dB | Phase = ---°", tag="txt_serial_timing", color=(200, 200, 200))

                            # Cmd tab (command selector - manual firmware commands)
                            with dpg.tab(label="Cmd", tag="tab_cmd"):
                                dpg.add_spacer(height=4)
                                dpg.add_text("Command Selector", color=(200, 200, 255))
                                dpg.add_separator()
                                dpg.add_spacer(height=4)
                                dpg.add_combo(label="Command", tag="cmd_selector", width=200,
                                              items=["SINE (0x01)", "CONSTANT (0x06)", "CENTER (0x08)",
                                                     "SCALE (0x09)", "SELFTEST (0xFE)", "STOP (0xFF)"],
                                              default_value="SINE (0x01)", callback=self._on_cmd_selector_change)
                                dpg.add_spacer(height=4)
                                dpg.add_input_float(label="Frequency (Hz)", tag="cmd_freq", default_value=20.0,
                                                    min_value=1.0, max_value=10000.0, step=0, width=150)
                                dpg.add_slider_int(label="Magnitude %", tag="cmd_mag_pct", default_value=10,
                                                   min_value=0, max_value=100, width=200)
                                with dpg.group(horizontal=True):
                                    dpg.add_input_float(label="Max Torque (Nm)", tag="cmd_max_torque",
                                                        default_value=25.0, min_value=0.1, max_value=100.0,
                                                        step=0, width=110)
                                    dpg.add_text("(manual)", color=(140, 140, 140))
                                with dpg.group(horizontal=True):
                                    dpg.add_input_int(label="Settle", tag="cmd_settle", default_value=2,
                                                      min_value=0, max_value=4, width=70)
                                    dpg.add_spacer(width=8)
                                    dpg.add_input_int(label="Ramp", tag="cmd_ramp", default_value=1,
                                                      min_value=0, max_value=1, width=70)
                                dpg.add_spacer(height=8)
                                dpg.add_separator()
                                dpg.add_spacer(height=4)
                                with dpg.group(horizontal=True):
                                    dpg.add_button(label="Send", tag="btn_cmd_send", width=90, callback=self._cmd_send)
                                    dpg.add_button(label="STOP", tag="btn_cmd_stop", width=80, callback=self._cmd_send_stop)
                                dpg.add_spacer(height=6)
                                dpg.add_text("Status: idle", tag="txt_cmd_status", wrap=350, color=(180, 220, 180))

                            # Inspector tab (second - loads by default)
                            # (built dynamically below via create_floating_inspector)

                            # Sweep tab (third - alphabetical)
                            with dpg.tab(label="Sweep", tag="tab_sweep"):
                                dpg.add_text("Frequency Range", color=(200, 200, 255))
                                dpg.add_separator()
                                dpg.add_input_float(label="Min (Hz)", tag="sweep_freq_min", default_value=10.0, step=0)
                                dpg.add_input_float(label="Max (Hz)", tag="sweep_freq_max", default_value=500.0, step=0)
                                dpg.add_input_int(label="Points", tag="sweep_num_freq", default_value=20, min_value=2, max_value=200)
                                dpg.add_separator()
                                dpg.add_text("Signal Configuration", color=(180, 180, 200))
                                dpg.add_separator()
                                with dpg.group(horizontal=True):
                                    dpg.add_input_float(label="Max Torque", tag="sweep_max_torque", default_value=25.0, width=200, min_value=0.1, max_value=100.0, step=0)
                                    dpg.add_text("(auto)", color=(140, 140, 140))
                                with dpg.group(horizontal=True):
                                    dpg.add_slider_int(label="Torque", tag="sweep_torque_pct", default_value=10, min_value=0, max_value=100, width=200, callback=self._on_sweep_torque_slider)
                                    dpg.add_text("10% (2.5 Nm)", tag="txt_sweep_torque_readout", color=(180, 220, 180))
                                dpg.add_input_int(label="Settle periods", tag="sweep_settling", default_value=2, min_value=0, max_value=4)
                                dpg.add_input_int(label="Ramp periods", tag="sweep_ramp", default_value=1, min_value=0, max_value=1)
                                dpg.add_spacer(height=8)
                                dpg.add_separator()
                                dpg.add_spacer(height=4)
                                with dpg.group(horizontal=True):
                                    dpg.add_text("Frequencies:", color=(200, 200, 255))
                                    dpg.add_button(label="Preview", width=80, callback=self._sweep_preview_freqs)
                                dpg.add_text("", tag="txt_sweep_freqs", wrap=350, color=(180, 220, 180))
                                dpg.add_spacer(height=4)
                                dpg.add_progress_bar(tag="sweep_progress", width=-1, default_value=0.0)
                                dpg.add_spacer(height=3)
                                dpg.add_text("Status: Ready", tag="txt_sweep_status", color=(200, 200, 200))
                                dpg.add_spacer(height=6)
                                dpg.add_button(label="Run", tag="btn_sweep_run", callback=self._sweep_run)
                                dpg.add_button(label="Stop", tag="btn_sweep_cancel", enabled=False, callback=self._sweep_cancel)
                                dpg.add_button(label="Export", tag="btn_sweep_export", enabled=False, callback=self._sweep_export)
            
                    # Log Section (Bottom Half)
                    with dpg.child_window(tag="panel_log", height=-1):
                        dpg.add_text("System Log")
                        dpg.add_listbox(tag="log_list", num_items=30, width=-1)

                self._configure_hover_defaults()
                self._set_mouse_status_visibility(self._mouse_status_visible)

        # Initialize Live Inspector (Docked in inspector_tab_bar)
        self.create_floating_inspector(None, parent="inspector_tab_bar")
        
        # FINAL BINDING
        try:
            dpg.set_item_drop_callback("Main", self.on_drop_receive)
        except Exception: pass

    def toggle_redlines(self, sender, app_data, user_data=None):
        self.show_redlines = bool(app_data)

    def get_settings_path(self):
        import os
        base_path = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_path, "user_settings.json")

    def load_settings(self):
        # Defaults
        self.window_width = 1280
        self.window_height = 800
        self.window_x = 100
        self.window_y = 100
        self.maximized = False
        self.current_theme_mode = "Dark Mode"
        
        path = self.get_settings_path()
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    settings = json.load(f)
                    self.window_width = settings.get("width", 1280)
                    self.window_height = settings.get("height", 800)
                    self.window_x = settings.get("x", 100)
                    self.window_y = settings.get("y", 100)
                    self.maximized = settings.get("maximized", False)
                    self.current_theme_mode = settings.get("theme", "Dark Mode")
                    
                    self.show_redlines = settings.get("show_redlines", True)
                    self._wheel_graph_visible = settings.get("wheel_graph_visible", True)
                    self._mouse_status_visible = settings.get("mouse_status_visible", True)
                    self.wheel_graph_gain = settings.get("wheel_graph_gain", 1.0)
                    self._device_torque_overrides = settings.get("device_torque_overrides", {})
                    
                    # Validate Coordinates (Fix for invisible window issue)
                    if self.window_x < -10000 or self.window_y < -10000:
                        self.window_x = 100
                        self.window_y = 100
                    
                    if self.window_width < 100: self.window_width = 800
                    if self.window_height < 100: self.window_height = 600
            except Exception as e:
                self.log(f"Failed to load settings: {e}")

    def save_settings(self):
        width = dpg.get_viewport_width()
        height = dpg.get_viewport_height()
        pos = dpg.get_viewport_pos()
        
        is_maximized = False
        is_minimized = False
        try:
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(None, "MotorBuster")
            if hwnd:
                if ctypes.windll.user32.IsZoomed(hwnd):
                    is_maximized = True
                if ctypes.windll.user32.IsIconic(hwnd):
                    is_minimized = True
        except: pass

        # Prevent saving invalid coordinates if minimized
        save_x = int(pos[0])
        save_y = int(pos[1])
        save_w = width
        save_h = height
        
        if is_minimized or save_x < -10000 or save_y < -10000:
             # Fallback to loaded settings (preserve last known good state)
             save_x = self.window_x
             save_y = self.window_y
             save_w = self.window_width
             save_h = self.window_height

        settings = {
            "width": save_w,
            "height": save_h,
            "x": save_x,
            "y": save_y,
            "maximized": is_maximized,
            "theme": getattr(self, "current_theme_mode", "Dark Mode"),
            "show_redlines": self.show_redlines,
            "wheel_graph_visible": self._wheel_graph_visible,
            "mouse_status_visible": self._mouse_status_visible,
            "wheel_graph_gain": self.wheel_graph_gain
        }
        
        try:
            with open(self.get_settings_path(), 'w') as f:
                json.dump(settings, f, indent=4)
        except Exception as e:
            print(f"Failed to save settings: {e}")

    def run(self):
        # Resolve absolute path for icon
        import os
        base_path = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(base_path, "assets", "icon.ico")
        
        # Disable VSync for Haptics
        # DECORATED=False for Borderless
        dpg.create_viewport(title='MotorBuster', width=self.window_width, height=self.window_height, 
                            x_pos=self.window_x, y_pos=self.window_y,
                            vsync=False, 
                            small_icon=icon_path, large_icon=icon_path, 
                            decorated=True) 
                            
        dpg.setup_dearpygui()
        dpg.show_viewport()
        engine.set_directinput_hwnd(self._get_viewport_hwnd())
        
        # Restore Maximized State / Apply Theme
        if self.maximized:
            try:
                import ctypes
                hwnd = ctypes.windll.user32.FindWindowW(None, "MotorBuster")
                if hwnd:
                    ctypes.windll.user32.ShowWindow(hwnd, 3) # SW_MAXIMIZE
            except:
                pass
        
        self.apply_theme(self.current_theme_mode)

        dpg.set_primary_window("Main", True)
        
        # Native title bar already provides resizing — skip borderless resize hack
        # self._enable_resize()

        self.scan_devices()

        import win32gui
        import win32api
        
        self.frame_count = 0

        # Precise Timing Loop
        import time
        from time import perf_counter
        import ctypes
        
        # Enable high resolution timer for Windows to allow granular sleeps (1ms)
        try:
            winmm = ctypes.windll.winmm
            winmm.timeBeginPeriod(1)
            high_res_timer = True
        except:
            high_res_timer = False

        # Decoupled Loop State
        UI_TARGET_FPS = 60
        ui_interval = 1.0 / UI_TARGET_FPS
        last_ui_time = 0.0
        
        last_effects_time = 0.0

        # Input Polling State (High Freq)
        POLL_TARGET_HZ = 500 # 500Hz Polling (sufficient for high precision without spin-wait)
        poll_interval = 1.0 / POLL_TARGET_HZ
        last_poll_time = 0.0

        
        try:
            while dpg.is_dearpygui_running():
                now = perf_counter()
                
                # --- 1. Determine Target Rates ---
                is_interacting = (getattr(self.sequencer, 'is_scrubbing', False) or 
                                  self.sequencer.drag_clip is not None or 
                                  self.sequencer.resize_clip is not None)

                if self.sequencer.is_playing:
                    # Active Mode: Use User Selected Rate
                    try:
                        target_rate = engine.target_update_rate_hz
                    except: 
                        target_rate = 500
                    effects_interval = 1.0 / max(10, target_rate)
                elif self._drag_fps_override_hz > 0:
                    effects_interval = 1.0 / float(self._drag_fps_override_hz)
                elif is_interacting:
                    # Interaction Mode: High rate for smooth UI feedback
                    effects_interval = 1.0 / 120.0
                else:
                    # Idle Mode: Low rate for basic connectivity/monitoring (saves CPU)
                    effects_interval = 1.0 / 20.0 # 20 Hz when idle
                
                # --- 2. Check Timers ---
                time_since_ui = now - last_ui_time
                time_since_effects = now - last_effects_time
                time_since_poll = now - last_poll_time
                
                should_run_ui = time_since_ui >= ui_interval
                should_run_effects = time_since_effects >= effects_interval
                should_run_poll = time_since_poll >= poll_interval

                # --- 2a. Input Polling (Highest Priority, independent of effects) ---
                if should_run_poll:
                    if self._wheel_graph_visible:
                         engine.poll_input()
                         
                         rec_time = self.sequencer.current_time
                         if self.sequencer.is_playing:
                             # Use time.time() because last_tick uses time.time()
                             dt_tick = time.time() - self.sequencer.last_tick
                             rec_time += dt_tick
                         
                         self._record_wheel_sample(rec_time)
                         self._record_force_sample(rec_time)
                    
                    last_poll_time = now
                
                # --- 3. Effects & Input Loop (High Priority) ---
                if should_run_effects:
                    try:
                        self.update_loop()
                    except Exception as e:
                        print(f"Update Loop Crash: {e}")
                        self.log(f"CRITICAL: {e}")
                        self.sequencer.is_playing = False
                        
                    # Process SDL Event Queue (NEW: Execute queued API calls)
                    engine.process_event_queue(max_events=32)
                    
                    # Advance timer (drift correction: add interval, but don't fall behind if laggy)
                    # Simple reset to 'now' avoids "catching up" spiral
                    last_effects_time = now


                # --- 4. UI Loop (60Hz) ---
                if should_run_ui:
                    dpg.set_value("time_display", f"{self.sequencer.current_time:.2f}s")
                    
                    # Only re-render timeline if it's visible (optimization)
                    self.render_timeline() 
                    
                    # Update Cursor Visuals (if not handled in effects loop this frame)
                    if not should_run_effects:
                         mpos = dpg.get_mouse_pos(local=False)
                         rel_x, rel_y = self.get_canvas_relative_pos(mpos)
                         if self._is_mouse_in_timeline_viewport(mpos):
                             track_idx = self._track_index_from_rel_y(rel_y)
                             resize_clip_hover, resize_edge = self._get_resize_hover(track_idx, rel_x)
                             should_hide_system = bool(resize_clip_hover or self.sequencer.resize_clip)
                             if should_hide_system:
                                 self._draw_resize_cursor(rel_x, rel_y)
                             self._set_system_cursor_visible(not should_hide_system)

                    dpg.render_dearpygui_frame()
                    last_ui_time = now
                    
                    # FPS Stats
                    self.frame_count += 1
                    try:
                        if not hasattr(self, 'fps_last_time_stat'):
                             self.fps_last_time_stat = time.time()
                             self.fps_frames_stat = 0
                        self.fps_frames_stat += 1
                        if time.time() - self.fps_last_time_stat >= 1.0:
                             self.fps_frames_stat = 0
                             self.fps_last_time_stat = time.time()
                    except: pass

                # --- 5. Sleep Schedule ---
                # Calculate time to next event
                next_ui = last_ui_time + ui_interval
                next_effects = last_effects_time + effects_interval
                next_poll = last_poll_time + poll_interval
                next_event = min(next_ui, next_effects, next_poll)
                
                sleep_duration = next_event - perf_counter()
                
                if sleep_duration > 0:
                    # Tuned sleep threshold: 0.5ms if high resolution timer is enabled.
                    # This allows sleeping for 1ms intervals (approx) without spinning.
                    sleep_threshold = 0.002 if not high_res_timer else 0.0005
                    if sleep_duration > sleep_threshold:
                        time.sleep(sleep_duration - 0.0005) # Sleep most of the time, spin remaining ~0.5ms
                    else:
                        pass # Spin wait for precision
        finally:
            if high_res_timer:
                try:
                    winmm.timeEndPeriod(1)
                except: pass
                
            self.save_settings()
            try:

                engine.stop_effect()

                self.log_api("stop_effect", {"scope": "all"})
            except Exception as e:
                self.log(f"Stop on exit failed: {e}")
            try:
                engine.close_device()
            except Exception as e:
                self.log(f"Close device on exit failed: {e}")
            dpg.destroy_context()

if __name__ == "__main__":
    app = MotorBusterNativeApp()
    app.run()
