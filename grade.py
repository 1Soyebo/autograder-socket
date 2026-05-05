#!/usr/bin/env python3
"""
EE450 Socket Programming Project Autograder (Spring 2026)

Scoring follows "EE 450 Testing Criteria.pdf":
  Phase A1  Boot-up (4 servers)           40 pts  (10 each)
  Phase 1B  Authentication (3 cases)      10 pts  (4+3+3)
  Phase 2   lookup                         2 pts
  Phase 2   lookup <doctor> (3 cases)      8 pts  (3+3+2)
  Phase 2   schedule (4 cases)            10 pts  (3+2+2+3)
  Phase 2   view_appointment (2 cases)     6 pts  (3+3)
  Phase 2   view_appointments (2 cases)    6 pts  (3+3)
  Phase 2   cancel (2 cases)               4 pts  (2+2)
  Phase 3   prescribe                      4 pts
  Phase 3   view_prescription patient (3)  6 pts  (2+2+2)
  Phase 3   view_prescription doctor (2)   4 pts  (2+2)
  -------------------------------------------------------
  TOTAL (before deductions)              100 pts

Deductions:
  -3 each : wrong / hardcoded static port numbers
  -3      : cancel removes the timeslot line from appointments.txt
  -1      : prescribe does not free the appointment slot

Usage:
    python3 grade.py <submission_dir> --usc-id <last_3_digits> [--verbose]

Example:
    python3 grade.py ./ee450_Doe_John --usc-id 319 --verbose
"""

import argparse
import datetime
import hashlib
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# SHA-256 utilities
# ---------------------------------------------------------------------------

def sha256_hash(text: str) -> str:
    """SHA-256 hex digest (strips whitespace), matching the project spec."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def hash_suffix(text: str) -> str:
    """Last 5 hex chars of the SHA-256 hash -- the hash_suffix per spec."""
    return sha256_hash(text)[-5:]


# ---------------------------------------------------------------------------
# Non-blocking stdout reader
# ---------------------------------------------------------------------------

class OutputReader:
    """Reads stdout of a subprocess in a background thread."""

    def __init__(self, proc: subprocess.Popen, name: str):
        self.proc = proc
        self.name = name
        self._q: queue.Queue = queue.Queue()
        self._lines: list = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self):
        for line in self.proc.stdout:
            with self._lock:
                self._lines.append(line)
            self._q.put(line)

    def snapshot(self) -> int:
        """Return current line count -- use as a before marker."""
        with self._lock:
            return len(self._lines)

    def new_output(self, since: int) -> str:
        """Return all output collected after position since."""
        with self._lock:
            return "".join(self._lines[since:])

    def all_output(self) -> str:
        with self._lock:
            return "".join(self._lines)

    def wait_for(self, pattern: str, timeout: float = 8.0) -> bool:
        """Block until pattern (case-insensitive) appears or timeout expires."""
        pat = re.compile(re.escape(pattern), re.IGNORECASE)
        if pat.search(self.all_output()):
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                line = self._q.get(timeout=min(remaining, 0.2))
                if pat.search(line):
                    return True
            except queue.Empty:
                pass
        return False


# ---------------------------------------------------------------------------
# Lenient message checkers
# ---------------------------------------------------------------------------

def contains_all(text: str, keywords: list) -> bool:
    """Return True if text contains every keyword (case-insensitive)."""
    lower = text.lower()
    return all(kw.lower() in lower for kw in keywords)


def contains_any(text: str, keywords: list) -> bool:
    lower = text.lower()
    return any(kw.lower() in lower for kw in keywords)


# ---------------------------------------------------------------------------
# Stdout tee -- writes to both the terminal and a log file simultaneously
# ---------------------------------------------------------------------------

class _Tee:
    """Wraps a stream so that writes go to both *stream* and *log_file*."""

    def __init__(self, stream, log_file):
        self._stream   = stream
        self._log_file = log_file

    def write(self, data):
        self._stream.write(data)
        self._log_file.write(data)

    def flush(self):
        self._stream.flush()
        self._log_file.flush()

    # Proxy all other attribute access to the underlying stream so that
    # anything that checks sys.stdout.encoding, isatty(), etc. still works.
    def __getattr__(self, name):
        return getattr(self._stream, name)


# ---------------------------------------------------------------------------
# Dummy reader for missing servers
# ---------------------------------------------------------------------------

class _DummyReader:
    def all_output(self):          return ""
    def snapshot(self):            return 0
    def new_output(self, since):   return ""


# ---------------------------------------------------------------------------
# Main Grader
# ---------------------------------------------------------------------------

class Grader:

    def __init__(self, submission_dir: str, usc_suffix: str, verbose: bool = False):
        self.submission_dir = Path(submission_dir).resolve()
        self.usc_suffix = usc_suffix.strip().zfill(3)[-3:]
        self.verbose = verbose

        self.score = 0
        self.deductions = 0
        self.feedback: list = []
        self._procs: list = []

        # Log file: <submission_dir_name>_<YYYYMMDD_HHMMSS>.txt  (next to grade.py)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^\w\-]", "_", self.submission_dir.name)
        self.log_path = Path(__file__).with_name(f"{safe_name}_{timestamp}.txt")

        n = int(self.usc_suffix)
        self.auth_udp_port  = 21000 + n
        self.presc_udp_port = 22000 + n
        self.appt_udp_port  = 23000 + n
        self.hosp_udp_port  = 25000 + n
        self.hosp_tcp_port  = 26000 + n

        # Test credentials
        self.doctor_name  = "alice"
        self.doctor_pass  = "doc123"
        self.patient_name = "bob"
        self.patient_pass = "pat456"
        self.bad_user     = "nobody"
        self.bad_pass     = "wrong"

        self.doctor_hs  = hash_suffix(self.doctor_name)
        self.patient_hs = hash_suffix(self.patient_name)
        self.bad_hs     = hash_suffix(self.bad_user)

        self.test_time      = "09:00"
        self.test_illness   = "flu"
        self.test_treatment = "Tamiflu"

        # Populated by check_phase1a
        self.readers: dict = {}

    # ------------------------------------------------------------------
    # Logging / recording
    # ------------------------------------------------------------------

    def _log(self, msg: str):
        if self.verbose:
            print(f"  [DBG] {msg}")

    def _record(self, label: str, earned: int, possible: int):
        if possible == 0:
            icon = "\u2139"
        elif earned == possible:
            icon = "\u2713"
        elif earned == 0:
            icon = "\u2717"
        else:
            icon = "~"
        line = f"  {icon} {label}: {earned}/{possible}"
        self.feedback.append(line)
        self.score += earned
        print(line)

    def _deduct(self, label: str, amount: int):
        line = f"  \u26a0 DEDUCTION -- {label}: -{amount}"
        self.feedback.append(line)
        self.deductions += amount
        print(line)

    def _section(self, title: str):
        print(f"\n{'='*62}")
        print(f"  {title}")
        print(f"{'='*62}")

    # ------------------------------------------------------------------
    # Process helpers
    # ------------------------------------------------------------------

    def _find_executable(self, name: str) -> list:
        for ext in ("", ".py"):
            p = self.submission_dir / (name + ext)
            if p.exists():
                return (["python3", str(p)] if ext == ".py" else [str(p)])
        return []

    def _start_process(self, cmd: list, name: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.submission_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._procs.append(proc)
        self._log(f"Started {name} (pid={proc.pid})")
        return proc

    def _kill_all(self):
        for p in self._procs:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self._procs.clear()

    # ------------------------------------------------------------------
    # Test data
    # ------------------------------------------------------------------

    def _create_test_data(self):
        with open(self.submission_dir / "users.txt", "w") as f:
            f.write(f"{sha256_hash(self.doctor_name)} {sha256_hash(self.doctor_pass)}\n")
            f.write(f"{sha256_hash(self.patient_name)} {sha256_hash(self.patient_pass)}\n")

        with open(self.submission_dir / "hospital.txt", "w") as f:
            f.write("[Doctors]\n")
            f.write(f"{self.doctor_name} {sha256_hash(self.doctor_name)}\n")
            f.write("[Treatments]\n")
            f.write(f"{self.test_illness} {self.test_treatment}\n")
            f.write("cold Rest\n")
            f.write("headache Aspirin\n")

        self._reset_appointments()
        self._reset_prescriptions()
        self._log("Test data files created")

    def _reset_appointments(self):
        with open(self.submission_dir / "appointments.txt", "w") as f:
            f.write(f"{self.doctor_name}\n")
            for hour in range(9, 17):
                f.write(f"{hour:02d}:00\n")
        time.sleep(0.2)

    def _reset_prescriptions(self):
        with open(self.submission_dir / "prescriptions.txt", "w") as f:
            f.write("")
        time.sleep(0.2)

    def _fill_all_appointments(self):
        """Fill all 8 time slots with dummy patients so the doctor is fully booked."""
        with open(self.submission_dir / "appointments.txt", "w") as f:
            f.write(f"{self.doctor_name}\n")
            for i, hour in enumerate(range(9, 17)):
                dummy = sha256_hash(f"dummy{i}")
                f.write(f"{hour:02d}:00 {dummy} cold\n")
        time.sleep(0.2)

    def _partially_fill_appointments(self):
        """Fill even-indexed slots, leave odd slots free."""
        with open(self.submission_dir / "appointments.txt", "w") as f:
            f.write(f"{self.doctor_name}\n")
            for i, hour in enumerate(range(9, 17)):
                if i % 2 == 0:
                    dummy = sha256_hash(f"dummy{i}")
                    f.write(f"{hour:02d}:00 {dummy} cold\n")
                else:
                    f.write(f"{hour:02d}:00\n")
        time.sleep(0.2)

    # ------------------------------------------------------------------
    # Client session runner
    # ------------------------------------------------------------------

    def _snapshots(self) -> dict:
        return {name: r.snapshot() for name, r in self.readers.items()}

    def _new_server_output(self, snaps: dict) -> dict:
        return {name: self.readers[name].new_output(snaps[name])
                for name in self.readers}

    def _run_client_session(
        self,
        args: list,
        commands: list,
        cmd_wait: float = 2.5,
        auth_wait: float = 1.5,
    ) -> tuple:
        """
        Run client with args, send commands, return (client_output, server_outputs_dict).
        """
        snaps = self._snapshots()
        cmd = self._find_executable("client")
        if not cmd:
            return "", {name: "" for name in self.readers}

        proc = self._start_process(cmd + list(args), "client")
        reader = OutputReader(proc, "client")
        time.sleep(auth_wait)

        for command in commands:
            self._log(f"  -> cmd: {command!r}")
            try:
                proc.stdin.write(command + "\n")
                proc.stdin.flush()
            except BrokenPipeError:
                break
            time.sleep(cmd_wait)

        try:
            proc.stdin.write("quit\n")
            proc.stdin.flush()
        except BrokenPipeError:
            pass
        time.sleep(0.5)
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            pass

        client_out = reader.all_output()
        server_outs = self._new_server_output(snaps)
        self._log(f"Client output:\n{client_out}")
        for sname, sout in server_outs.items():
            self._log(f"{sname} output:\n{sout}")
        return client_out, server_outs

    # ------------------------------------------------------------------
    # Phase 0 -- files + compile
    # ------------------------------------------------------------------

    def check_files(self) -> int:
        self._section("Phase 0 -- Required Files")

        components = {
            "client":                ["client.c","client.cc","client.cpp","client.py"],
            "hospital_server":       ["hospital_server.c","hospital_server.cc",
                                      "hospital_server.cpp","hospital_server.py"],
            "authentication_server": ["authentication_server.c","authentication_server.cc",
                                      "authentication_server.cpp","authentication_server.py"],
            "appointment_server":    ["appointment_server.c","appointment_server.cc",
                                      "appointment_server.cpp","appointment_server.py"],
            "prescription_server":   ["prescription_server.c","prescription_server.cc",
                                      "prescription_server.cpp","prescription_server.py"],
        }

        makefile_ok = (self.submission_dir / "Makefile").exists()
        readme_ok = any(
            (self.submission_dir / r).exists()
            for r in ["README","README.md","readme.txt","readme.md","README.txt"]
        )

        if not makefile_ok or not readme_ok:
            missing = []
            if not makefile_ok: missing.append("Makefile")
            if not readme_ok:   missing.append("README")
            self._record(
                f"Required files (MISSING: {', '.join(missing)}) -- WILL NOT GRADE", 0, 0
            )
            return 0

        missing_src = [
            c for c, variants in components.items()
            if not any((self.submission_dir / v).exists() for v in variants)
        ]
        if missing_src:
            self._record(f"Source files (missing: {', '.join(missing_src)})", 0, 5)
            return 0

        self._record("All required files present (Makefile, README, source files)", 5, 5)
        return 5

    def check_compile(self) -> int:
        self._section("Phase 0 -- Compilation (make all)")
        try:
            result = subprocess.run(
                ["make", "all"],
                cwd=str(self.submission_dir),
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            self._record("make all (timed out)", 0, 10)
            return 0
        except FileNotFoundError:
            self._record("make all (make not found)", 0, 10)
            return 0

        if result.returncode == 0:
            self._record("make all succeeded", 10, 10)
            return 10
        else:
            if self.verbose:
                for line in (result.stderr or result.stdout).splitlines()[-20:]:
                    print(f"    {line}")
            self._record("make all FAILED -- 5/100 cap per spec", 0, 10)
            return 0

    # ------------------------------------------------------------------
    # Phase A1 -- Boot-up (40 pts, 10 each)
    # ------------------------------------------------------------------

    def check_phase1a(self) -> int:
        self._section("Phase A1 -- Server Boot-Up Messages [40 pts]")
        total = 0

        boot_specs = [
            ("hospital_server",
             f"Hospital Server is up and running using UDP on port {self.hosp_udp_port}"),
            ("authentication_server",
             f"Authentication Server is up and running using UDP on port {self.auth_udp_port}"),
            ("appointment_server",
             f"Appointment Server is up and running using UDP on port {self.appt_udp_port}"),
            ("prescription_server",
             f"Prescription Server is up and running using UDP on port {self.presc_udp_port}"),
        ]

        self.readers = {}

        for name, expected in boot_specs:
            cmd = self._find_executable(name)
            if not cmd:
                self._record(f"{name} boot-up", 0, 10)
                continue
            proc = self._start_process(cmd, name)
            reader = OutputReader(proc, name)
            self.readers[name] = reader

            found = reader.wait_for(expected, timeout=8.0)
            self._log(f"{name} boot output:\n{reader.all_output()}")
            if found:
                self._record(f"{name} boot-up message", 10, 10)
                total += 10
            else:
                self._record(
                    f"{name} boot-up message (expected: \"{expected}\")", 0, 10
                )

        time.sleep(1.0)
        return total

    # ------------------------------------------------------------------
    # Phase 1B -- Authentication (10 pts: patient=4, doctor=3, fail=3)
    # ------------------------------------------------------------------

    def check_phase1b(self) -> int:
        self._section("Phase 1B -- Authentication [10 pts]")
        total = 0

        # Sub-case: Patient login success (4 pts)
        pts = 4
        c_out, s_outs = self._run_client_session(
            [self.patient_name, self.patient_pass], []
        )
        hosp = s_outs.get("hospital_server", "")
        auth = s_outs.get("authentication_server", "")
        client_ok = contains_all(c_out, ["authentication successful", "patient access"])
        hosp_ok   = contains_all(hosp, ["authentication request", self.patient_hs])
        auth_ok   = contains_any(auth, ["authentication succeeded", "succeeded"])
        if client_ok and hosp_ok and auth_ok:
            earned = pts
        elif client_ok and hosp_ok:
            earned = pts - 1
        elif client_ok:
            earned = pts - 2
        else:
            earned = 0
        self._record("Patient login success", earned, pts)
        total += earned

        # Sub-case: Doctor login success (3 pts)
        pts = 3
        c_out, s_outs = self._run_client_session(
            [self.doctor_name, self.doctor_pass], []
        )
        hosp = s_outs.get("hospital_server", "")
        auth = s_outs.get("authentication_server", "")
        client_ok = contains_all(c_out, ["authentication successful", "doctor access"])
        hosp_ok   = contains_all(hosp, ["authentication request", self.doctor_hs])
        auth_ok   = contains_any(auth, ["authentication succeeded", "succeeded"])
        if client_ok and hosp_ok and auth_ok:
            earned = pts
        elif client_ok and hosp_ok:
            earned = pts - 1
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record("Doctor login success", earned, pts)
        total += earned

        # Sub-case: Failed login (3 pts)
        pts = 3
        c_out, s_outs = self._run_client_session(
            [self.bad_user, self.bad_pass], []
        )
        auth = s_outs.get("authentication_server", "")
        hosp = s_outs.get("hospital_server", "")
        client_ok = contains_any(c_out, ["incorrect", "failed", "invalid", "credentials"])
        auth_ok   = contains_any(auth, ["authentication failed", "failed"])
        hosp_ok   = contains_any(hosp, ["authentication request"])
        if client_ok and auth_ok and hosp_ok:
            earned = pts
        elif client_ok and auth_ok:
            earned = pts - 1
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record("Failed login (invalid credentials)", earned, pts)
        total += earned

        return total

    # ------------------------------------------------------------------
    # Phase 2 -- lookup [2 pts]
    # ------------------------------------------------------------------

    def check_phase2_lookup(self) -> int:
        self._section("Phase 2 -- lookup (list doctors) [2 pts]")
        pts = 2
        c_out, s_outs = self._run_client_session(
            [self.patient_name, self.patient_pass], ["lookup"]
        )
        hosp = s_outs.get("hospital_server", "")
        appt = s_outs.get("appointment_server", "")
        client_ok = self.doctor_name.lower() in c_out.lower()
        hosp_ok   = contains_any(hosp, ["lookup request", "doctor lookup"])
        appt_ok   = contains_any(appt, ["availability request", "lookup result"])
        if client_ok and hosp_ok and appt_ok:
            earned = pts
        elif client_ok and hosp_ok:
            earned = pts - 1
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record("lookup (list all doctors)", earned, pts)
        return earned

    # ------------------------------------------------------------------
    # Phase 2 -- lookup <doctor> [8 pts: 3+3+2]
    # ------------------------------------------------------------------

    def check_phase2_lookup_doctor(self) -> int:
        self._section(f"Phase 2 -- lookup <doctor> [8 pts]")
        total = 0

        # Sub-case: all slots available (3 pts)
        pts = 3
        self._reset_appointments()
        c_out, s_outs = self._run_client_session(
            [self.patient_name, self.patient_pass], [f"lookup {self.doctor_name}"]
        )
        appt = s_outs.get("appointment_server", "")
        client_ok = contains_any(c_out, ["all time blocks are available", "all time blocks"])
        appt_ok   = contains_any(appt, ["all time blocks are available", "all time blocks"])
        if client_ok and appt_ok:
            earned = pts
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record(f"lookup {self.doctor_name} -- all slots available", earned, pts)
        total += earned

        # Sub-case: no slots available (3 pts)
        pts = 3
        self._fill_all_appointments()
        c_out, s_outs = self._run_client_session(
            [self.patient_name, self.patient_pass], [f"lookup {self.doctor_name}"]
        )
        appt = s_outs.get("appointment_server", "")
        client_ok = contains_any(c_out, ["no time slots available", "no time slots"])
        appt_ok   = contains_any(appt, ["no time slots available", "no time slots"])
        if client_ok and appt_ok:
            earned = pts
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record(f"lookup {self.doctor_name} -- no slots available", earned, pts)
        total += earned

        # Sub-case: some slots available (2 pts)
        pts = 2
        self._partially_fill_appointments()
        c_out, s_outs = self._run_client_session(
            [self.patient_name, self.patient_pass], [f"lookup {self.doctor_name}"]
        )
        appt = s_outs.get("appointment_server", "")
        client_ok = contains_any(c_out, ["available at times", "is available", "10:00", "12:00"])
        appt_ok   = contains_any(appt, ["some time slots available", "some time slots"])
        if client_ok and appt_ok:
            earned = pts
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record(f"lookup {self.doctor_name} -- some slots available", earned, pts)
        total += earned

        self._reset_appointments()
        return total

    # ------------------------------------------------------------------
    # Phase 2 -- schedule [10 pts: 3+2+2+3]
    # ------------------------------------------------------------------

    def check_phase2_schedule(self) -> int:
        self._section("Phase 2 -- schedule [10 pts]")
        total = 0

        # Sub-case: success (3 pts)
        pts = 3
        self._reset_appointments()
        c_out, s_outs = self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"]
        )
        appt = s_outs.get("appointment_server", "")
        hosp = s_outs.get("hospital_server", "")
        client_ok = contains_any(c_out, ["successfully scheduled", "appointment has been"])
        appt_ok   = contains_any(appt, ["scheduled successfully", "appointment has been scheduled"])
        hosp_ok   = contains_any(hosp, ["schedule request", "appointment"])
        if client_ok and appt_ok and hosp_ok:
            earned = pts
        elif client_ok and appt_ok:
            earned = pts - 1
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record("schedule -- success", earned, pts)
        total += earned
        self._reset_appointments()

        # Sub-case: outside valid hours (2 pts)
        pts = 2
        outside_time = "18:00"
        c_out, s_outs = self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {outside_time} {self.test_illness}"]
        )
        appt = s_outs.get("appointment_server", "")
        client_ok = contains_any(c_out, ["unable", "not available", "cannot"])
        appt_ok   = contains_any(appt, ["not available", "scheduling request"])
        if client_ok and appt_ok:
            earned = pts
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record("schedule -- outside valid hours (18:00)", earned, pts)
        total += earned

        # Sub-case: slot already occupied (2 pts)
        pts = 2
        self._reset_appointments()
        self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"]
        )
        c_out, s_outs = self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"]
        )
        appt = s_outs.get("appointment_server", "")
        client_ok = contains_any(c_out, ["unable", "not available", "cannot"])
        appt_ok   = contains_any(appt, ["not available", "scheduling request"])
        if client_ok and appt_ok:
            earned = pts
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record("schedule -- slot already occupied", earned, pts)
        total += earned
        self._reset_appointments()

        # Sub-case: all slots taken (3 pts)
        pts = 3
        self._fill_all_appointments()
        c_out, s_outs = self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"]
        )
        appt = s_outs.get("appointment_server", "")
        client_ok = contains_any(c_out, ["all time blocks have been taken", "unable", "no time slots"])
        appt_ok   = contains_any(appt, ["not available", "scheduling request"])
        if client_ok and appt_ok:
            earned = pts
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record("schedule -- all slots taken", earned, pts)
        total += earned
        self._reset_appointments()

        return total

    # ------------------------------------------------------------------
    # Phase 2 -- view_appointment (patient) [6 pts: 3+3]
    # ------------------------------------------------------------------

    def check_phase2_view_appointment(self) -> int:
        self._section("Phase 2 -- view_appointment (patient) [6 pts]")
        total = 0

        # Sub-case: appointment found (3 pts)
        pts = 3
        self._reset_appointments()
        self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"]
        )
        c_out, s_outs = self._run_client_session(
            [self.patient_name, self.patient_pass], ["view_appointment"]
        )
        appt = s_outs.get("appointment_server", "")
        hosp = s_outs.get("hospital_server", "")
        client_ok = contains_any(c_out, [self.doctor_name, self.test_time, "appointment scheduled"])
        appt_ok   = contains_any(appt, ["view appointment", self.patient_hs])
        hosp_ok   = contains_any(hosp, ["view appointment"])
        if client_ok and appt_ok and hosp_ok:
            earned = pts
        elif client_ok and appt_ok:
            earned = pts - 1
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record("view_appointment -- appointment found", earned, pts)
        total += earned
        self._reset_appointments()

        # Sub-case: no appointment (3 pts)
        pts = 3
        c_out, s_outs = self._run_client_session(
            [self.patient_name, self.patient_pass], ["view_appointment"]
        )
        appt = s_outs.get("appointment_server", "")
        client_ok = contains_any(c_out, ["do not have", "no appointment", "you do not"])
        appt_ok   = contains_any(appt, ["no appointment", "has no appointment"])
        if client_ok and appt_ok:
            earned = pts
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record("view_appointment -- no appointment", earned, pts)
        total += earned

        return total

    # ------------------------------------------------------------------
    # Phase 2 -- view_appointments (doctor) [6 pts: 3+3]
    # ------------------------------------------------------------------

    def check_phase2_view_appointments_doctor(self) -> int:
        self._section("Phase 2 -- view_appointments (doctor) [6 pts]")
        total = 0

        # Sub-case: one or more appointments (3 pts)
        pts = 3
        self._reset_appointments()
        self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"]
        )
        c_out, s_outs = self._run_client_session(
            [self.doctor_name, self.doctor_pass], ["view_appointments"]
        )
        appt = s_outs.get("appointment_server", "")
        hosp = s_outs.get("hospital_server", "")
        client_ok = contains_any(c_out, [self.test_time, "scheduled at times", "is scheduled"])
        appt_ok   = contains_any(appt, [self.doctor_name, "view appointments", "scheduled for"])
        hosp_ok   = contains_any(hosp, ["view appointments", self.doctor_name])
        if client_ok and appt_ok and hosp_ok:
            earned = pts
        elif client_ok and appt_ok:
            earned = pts - 1
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record("view_appointments (doctor) -- has appointments", earned, pts)
        total += earned
        self._reset_appointments()

        # Sub-case: no appointments (3 pts)
        pts = 3
        c_out, s_outs = self._run_client_session(
            [self.doctor_name, self.doctor_pass], ["view_appointments"]
        )
        appt = s_outs.get("appointment_server", "")
        client_ok = contains_any(c_out, ["do not have any", "no appointments", "you do not"])
        appt_ok   = contains_any(appt, ["no appointments have been made", "no appointments",
                                        self.doctor_name])
        if client_ok and appt_ok:
            earned = pts
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record("view_appointments (doctor) -- no appointments", earned, pts)
        total += earned

        return total

    # ------------------------------------------------------------------
    # Phase 2 -- cancel [4 pts: 2+2]
    # ------------------------------------------------------------------

    def check_phase2_cancel(self) -> int:
        self._section("Phase 2 -- cancel [4 pts]")
        total = 0

        # Sub-case: successful cancel (2 pts)
        pts = 2
        self._reset_appointments()
        self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"]
        )
        c_out, s_outs = self._run_client_session(
            [self.patient_name, self.patient_pass], ["cancel"]
        )
        appt = s_outs.get("appointment_server", "")
        hosp = s_outs.get("hospital_server", "")
        client_ok = contains_any(c_out, ["successfully cancelled", "cancelled your appointment"])
        appt_ok   = contains_any(appt, ["successfully cancelled", "cancel appointment"])
        hosp_ok   = contains_any(hosp, ["cancel request", "cancel"])
        if client_ok and appt_ok and hosp_ok:
            earned = pts
        elif client_ok and appt_ok:
            earned = pts - 1
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record("cancel -- success", earned, pts)
        total += earned

        # Deduction: timeslot line must remain after cancel
        try:
            with open(self.submission_dir / "appointments.txt") as f:
                content = f.read()
            if self.test_time not in content:
                self._deduct("cancel removes timeslot line from appointments.txt", 3)
        except Exception:
            pass

        # Sub-case: no appointment to cancel (2 pts)
        pts = 2
        self._reset_appointments()
        c_out, s_outs = self._run_client_session(
            [self.patient_name, self.patient_pass], ["cancel"]
        )
        appt = s_outs.get("appointment_server", "")
        client_ok = contains_any(c_out, ["no appointments", "you have no", "failed"])
        appt_ok   = contains_any(appt, ["failed to find", "error", "cancel appointment"])
        if client_ok and appt_ok:
            earned = pts
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record("cancel -- no appointment found", earned, pts)
        total += earned

        return total

    # ------------------------------------------------------------------
    # Phase 3 -- prescribe (doctor) [4 pts]
    # ------------------------------------------------------------------

    def check_phase3_prescribe(self) -> int:
        self._section("Phase 3 -- prescribe (doctor) [4 pts]")
        pts = 4

        self._reset_appointments()
        self._reset_prescriptions()
        self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"]
        )

        c_out, s_outs = self._run_client_session(
            [self.doctor_name, self.doctor_pass],
            [f"prescribe {self.patient_name} Daily"]
        )
        hosp  = s_outs.get("hospital_server", "")
        appt  = s_outs.get("appointment_server", "")
        presc = s_outs.get("prescription_server", "")

        client_ok = contains_any(c_out, ["successfully prescribed", self.test_treatment.lower()])
        hosp_ok   = contains_any(hosp, ["prescription request", "prescribe"])
        appt_ok   = contains_any(appt, ["sending back", "successfully removed", self.patient_hs])
        presc_ok  = contains_any(presc, ["prescription", self.doctor_name, self.patient_hs])

        if client_ok and hosp_ok and appt_ok and presc_ok:
            earned = pts
        elif client_ok and hosp_ok and appt_ok:
            earned = pts - 1
        elif client_ok and hosp_ok:
            earned = pts - 2
        elif client_ok:
            earned = pts - 2
        else:
            earned = 0
        self._record("prescribe (success)", earned, pts)

        # Deduction: prescribe must free the appointment slot
        try:
            with open(self.submission_dir / "appointments.txt") as f:
                content = f.read()
            if self.patient_hs in content:
                self._deduct("prescribe did not free the appointment slot", 1)
        except Exception:
            pass

        return earned

    # ------------------------------------------------------------------
    # Phase 3 -- view_prescription (patient) [6 pts: 2+2+2]
    # ------------------------------------------------------------------

    def check_phase3_view_prescription_patient(self) -> int:
        self._section("Phase 3 -- view_prescription (patient) [6 pts]")
        total = 0

        # Sub-case: active prescription exists (2 pts)
        pts = 2
        self._reset_appointments()
        self._reset_prescriptions()
        self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"]
        )
        self._run_client_session(
            [self.doctor_name, self.doctor_pass],
            [f"prescribe {self.patient_name} Daily"]
        )
        c_out, s_outs = self._run_client_session(
            [self.patient_name, self.patient_pass], ["view_prescription"]
        )
        presc = s_outs.get("prescription_server", "")
        client_ok = contains_any(c_out, [self.test_treatment.lower(), "prescribed", "daily"])
        presc_ok  = contains_any(presc, ["prescription exists", "a prescription exists"])
        if client_ok and presc_ok:
            earned = pts
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record("view_prescription (patient) -- prescription exists", earned, pts)
        total += earned

        # Sub-case: frequency = None (2 pts)
        pts = 2
        self._reset_appointments()
        self._reset_prescriptions()
        self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"]
        )
        self._run_client_session(
            [self.doctor_name, self.doctor_pass],
            [f"prescribe {self.patient_name} None"]
        )
        c_out, s_outs = self._run_client_session(
            [self.patient_name, self.patient_pass], ["view_prescription"]
        )
        presc = s_outs.get("prescription_server", "")
        client_ok = contains_any(c_out, ["were not prescribed", "not prescribed", "none"])
        presc_ok  = contains_any(presc, ["no current prescriptions", "there are no current"])
        if client_ok and presc_ok:
            earned = pts
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record("view_prescription (patient) -- frequency is None", earned, pts)
        total += earned

        # Sub-case: no prescription record (2 pts)
        pts = 2
        self._reset_prescriptions()
        self._reset_appointments()
        c_out, s_outs = self._run_client_session(
            [self.patient_name, self.patient_pass], ["view_prescription"]
        )
        presc = s_outs.get("prescription_server", "")
        client_ok = contains_any(c_out, ["do not have a prescription", "no prescription",
                                         "you do not"])
        presc_ok  = contains_any(presc, ["no current prescriptions", "there are no current"])
        if client_ok and presc_ok:
            earned = pts
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record("view_prescription (patient) -- no prescription record", earned, pts)
        total += earned

        return total

    # ------------------------------------------------------------------
    # Phase 3 -- view_prescription (doctor) [4 pts: 2+2]
    # ------------------------------------------------------------------

    def check_phase3_view_prescription_doctor(self) -> int:
        self._section("Phase 3 -- view_prescription <patient> (doctor) [4 pts]")
        total = 0

        # Sub-case: prescription exists (2 pts)
        pts = 2
        self._reset_appointments()
        self._reset_prescriptions()
        self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"]
        )
        self._run_client_session(
            [self.doctor_name, self.doctor_pass],
            [f"prescribe {self.patient_name} Daily"]
        )
        c_out, s_outs = self._run_client_session(
            [self.doctor_name, self.doctor_pass],
            [f"view_prescription {self.patient_name}"]
        )
        presc = s_outs.get("prescription_server", "")
        client_ok = contains_any(c_out, [self.test_treatment.lower(), "prescribed", "daily",
                                         self.patient_name])
        presc_ok  = contains_any(presc, ["prescription exists", "a prescription exists"])
        if client_ok and presc_ok:
            earned = pts
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record(f"view_prescription {self.patient_name} (doctor) -- exists", earned, pts)
        total += earned

        # Sub-case: no prescription (2 pts)
        pts = 2
        self._reset_prescriptions()
        self._reset_appointments()
        c_out, s_outs = self._run_client_session(
            [self.doctor_name, self.doctor_pass],
            [f"view_prescription {self.patient_name}"]
        )
        presc = s_outs.get("prescription_server", "")
        client_ok = contains_any(c_out, ["does not have a prescription", "no prescription",
                                         self.patient_name])
        presc_ok  = contains_any(presc, ["no current prescriptions", "there are no current"])
        if client_ok and presc_ok:
            earned = pts
        elif client_ok:
            earned = pts - 1
        else:
            earned = 0
        self._record(f"view_prescription {self.patient_name} (doctor) -- no record", earned, pts)
        total += earned

        return total

    # ------------------------------------------------------------------
    # Deductions -- port number correctness
    # ------------------------------------------------------------------

    def check_port_deductions(self):
        self._section("Deductions -- Static Port Numbers")
        n = int(self.usc_suffix)
        checks = [
            ("authentication_server", self.auth_udp_port,  "authentication_server"),
            ("prescription_server",   self.presc_udp_port, "prescription_server"),
            ("appointment_server",    self.appt_udp_port,  "appointment_server"),
            ("hospital_server (UDP)", self.hosp_udp_port,  "hospital_server"),
            ("hospital_server (TCP)", self.hosp_tcp_port,  "hospital_server"),
        ]
        for label, expected_port, reader_key in checks:
            output = self.readers.get(reader_key, _DummyReader()).all_output()
            if output and str(expected_port) not in output:
                self._deduct(f"Wrong port for {label} (expected {expected_port})", 3)
            else:
                print(f"  \u2713 Port {expected_port} for {label}: OK")

    # ------------------------------------------------------------------
    # Main runner
    # ------------------------------------------------------------------

    def run(self) -> int:
        log_file = open(self.log_path, "w", encoding="utf-8")
        original_stdout = sys.stdout
        sys.stdout = _Tee(original_stdout, log_file)
        try:
            return self._run_grading()
        finally:
            sys.stdout = original_stdout
            log_file.close()

    def _run_grading(self) -> int:
        print(f"\n{'#'*62}")
        print(f"  EE450 Socket Programming Autograder -- Spring 2026")
        print(f"  Submission : {self.submission_dir}")
        print(f"  USC suffix : {self.usc_suffix}")
        print(f"  Ports      : auth={self.auth_udp_port}, appt={self.appt_udp_port},")
        print(f"               presc={self.presc_udp_port}, hosp_udp={self.hosp_udp_port},")
        print(f"               hosp_tcp={self.hosp_tcp_port}")
        print(f"{'#'*62}\n")

        try:
            file_pts = self.check_files()
            if file_pts == 0:
                print("\n  *** Missing Makefile or README -- SUBMISSION WILL NOT BE GRADED ***")
                self._print_summary()
                return 0

            compile_pts = self.check_compile()
            if compile_pts == 0:
                self.score = 5
                print("\n  *** Compilation failed -- 5/100 cap per spec ***")
                self._print_summary()
                return self.score

            self._create_test_data()
            self.check_phase1a()

            if not self.readers:
                print("\n  *** No servers could be started -- 10/100 cap per spec ***")
                self.score = min(self.score, 10)
                self._print_summary()
                return self.score

            self.check_phase1b()
            self.check_phase2_lookup()
            self.check_phase2_lookup_doctor()
            self.check_phase2_schedule()
            self.check_phase2_view_appointment()
            self.check_phase2_view_appointments_doctor()
            self.check_phase2_cancel()
            self.check_phase3_prescribe()
            self.check_phase3_view_prescription_patient()
            self.check_phase3_view_prescription_doctor()
            self.check_port_deductions()

        finally:
            self._kill_all()

        return self._print_summary()

    def _print_summary(self) -> int:
        print(f"\n{'='*62}")
        print("  GRADING SUMMARY")
        print(f"{'='*62}")
        for line in self.feedback:
            print(line)
        gross = self.score
        net   = max(0, gross - self.deductions)
        print(f"\n  {'--'*23}")
        print(f"  Gross score  : {gross} / 100")
        if self.deductions:
            print(f"  Deductions   : -{self.deductions}")
        print(f"  FINAL SCORE  : {net} / 100")
        print(f"{'='*62}")
        print(f"  Log saved to : {self.log_path}")
        print(f"{'='*62}\n")
        return net


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="EE450 Spring 2026 Socket Programming Project Autograder"
    )
    parser.add_argument("submission_dir",
                        help="Path to the extracted submission directory")
    parser.add_argument("--usc-id", dest="usc_id", default="000",
                        help="Last 3 digits of student USC ID (default: 000)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print debug output from all processes")
    args = parser.parse_args()

    grader = Grader(
        submission_dir=args.submission_dir,
        usc_suffix=args.usc_id,
        verbose=args.verbose,
    )
    sys.exit(grader.run())


if __name__ == "__main__":
    main()
