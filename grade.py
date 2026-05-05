#!/usr/bin/env python3
"""
EE450 Socket Programming Project Autograder (Spring 2026)

Usage:
    python3 grade.py <submission_dir> --usc-id <last_3_digits> [--verbose]

Example:
    python3 grade.py ./ee450_Doe_John --usc-id 319 --verbose

The script:
  1. Checks required files (source files, Makefile, README)
  2. Compiles with `make all`
  3. Creates test data files (users.txt, hospital.txt, appointments.txt, prescriptions.txt)
  4. Starts servers in required order and checks boot-up messages
  5. Runs client sessions testing Phase 1B, Phase 2, and Phase 3
  6. Grades each phase against expected on-screen messages
  7. Prints a final score breakdown
"""

import argparse
import hashlib
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sha256_hash(text: str) -> str:
    """SHA-256 hex digest of text (stripped), matching the project spec."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def hash_suffix(text: str) -> str:
    """Last 5 hex characters of the SHA-256 hash (the 'hash_suffix' per spec)."""
    return sha256_hash(text)[-5:]


# ---------------------------------------------------------------------------
# Non-blocking stdout reader
# ---------------------------------------------------------------------------

class OutputReader:
    """Reads stdout of a subprocess in a background thread into a queue."""

    def __init__(self, proc: subprocess.Popen, name: str):
        self.proc = proc
        self.name = name
        self._q: queue.Queue = queue.Queue()
        self._lines: list = []
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self):
        for line in self.proc.stdout:
            self._q.put(line)
            self._lines.append(line)

    def get_lines(self, timeout: float = 0.0) -> list:
        """Drain newly-arrived lines within `timeout` seconds."""
        deadline = time.time() + timeout
        new_lines = []
        while True:
            remaining = deadline - time.time()
            try:
                line = self._q.get(timeout=max(remaining, 0.01))
                new_lines.append(line)
            except queue.Empty:
                break
        return new_lines

    def all_output(self) -> str:
        """Return all output collected so far."""
        return "".join(self._lines)

    def wait_for(self, pattern: str, timeout: float = 5.0) -> bool:
        """Block until *pattern* appears in output or timeout expires."""
        deadline = time.time() + timeout
        compiled = re.compile(re.escape(pattern))
        # First check already-collected lines
        if compiled.search(self.all_output()):
            return True
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                line = self._q.get(timeout=min(remaining, 0.2))
                self._lines.append(line)
                if compiled.search(line):
                    return True
            except queue.Empty:
                pass
        return False


# ---------------------------------------------------------------------------
# Grader
# ---------------------------------------------------------------------------

class Grader:

    # Grading weights
    WEIGHTS = {
        "files":    5,   # required files present
        "compile":  10,  # make all succeeds
        "phase1a":  10,  # boot-up messages
        "phase1b":  20,  # authentication
        "phase2":   30,  # patient + doctor Phase-2 commands
        "phase3":   25,  # prescription Phase-3 commands
    }

    def __init__(self, submission_dir: str, usc_suffix: str, verbose: bool = False):
        self.submission_dir = Path(submission_dir).resolve()
        # Accept 1-3 digit suffix; zero-pad to 3 digits
        self.usc_suffix = usc_suffix.strip().zfill(3)[-3:]
        self.verbose = verbose

        self.score = 0
        self.feedback: list = []
        self._procs: list = []        # all started subprocesses

        # Ports derived from USC ID suffix
        n = int(self.usc_suffix)
        self.auth_udp_port   = 21000 + n
        self.presc_udp_port  = 22000 + n
        self.appt_udp_port   = 23000 + n
        self.hosp_udp_port   = 25000 + n
        self.hosp_tcp_port   = 26000 + n

        # Test credentials
        self.doctor_name   = "alice"
        self.doctor_pass   = "doc123"
        self.patient_name  = "bob"
        self.patient_pass  = "pat456"
        self.bad_user      = "nobody"
        self.bad_pass      = "wrong"

        # Pre-compute hashes
        self.doctor_hash         = sha256_hash(self.doctor_name)
        self.doctor_hash_suffix  = hash_suffix(self.doctor_name)
        self.patient_hash        = sha256_hash(self.patient_name)
        self.patient_hash_suffix = hash_suffix(self.patient_name)

        # Appointment test data
        self.test_doctor    = self.doctor_name
        self.test_time      = "09:00"
        self.test_illness   = "flu"
        self.test_treatment = "Tamiflu"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log(self, msg: str):
        if self.verbose:
            print(f"  [DBG] {msg}")

    def _record(self, label: str, earned: int, possible: int):
        if possible == 0:
            icon = "ℹ"
        elif earned == possible:
            icon = "✓"
        elif earned == 0:
            icon = "✗"
        else:
            icon = "~"
        line = f"  {icon} {label}: {earned}/{possible}"
        self.feedback.append(line)
        self.score += earned
        print(line)

    def _section(self, title: str):
        print(f"\n{'='*60}")
        print(f"  {title}")
        print(f"{'='*60}")

    def _find_executable(self, name: str) -> list:
        """Return a command list to run the named component."""
        for ext in ("", ".py"):
            p = self.submission_dir / (name + ext)
            if p.exists():
                if ext == ".py":
                    return ["python3", str(p)]
                return [str(p)]
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
    # Test data creation
    # ------------------------------------------------------------------

    def _create_test_data(self):
        """Write the four data files the servers read on start-up."""

        # users.txt  (authentication_server)
        with open(self.submission_dir / "users.txt", "w") as f:
            f.write(f"{sha256_hash(self.doctor_name)} {sha256_hash(self.doctor_pass)}\n")
            f.write(f"{sha256_hash(self.patient_name)} {sha256_hash(self.patient_pass)}\n")

        # hospital.txt  (hospital_server)
        with open(self.submission_dir / "hospital.txt", "w") as f:
            f.write("[Doctors]\n")
            f.write(f"{self.doctor_name} {sha256_hash(self.doctor_name)}\n")
            f.write("[Treatments]\n")
            f.write(f"{self.test_illness} {self.test_treatment}\n")
            f.write("cold Rest\n")
            f.write("headache Aspirin\n")

        # appointments.txt  (appointment_server)
        with open(self.submission_dir / "appointments.txt", "w") as f:
            f.write(f"{self.doctor_name}\n")
            for hour in range(9, 17):
                f.write(f"{hour:02d}:00\n")

        # prescriptions.txt  (prescription_server) – start empty
        with open(self.submission_dir / "prescriptions.txt", "w") as f:
            f.write("")

        self._log("Test data files created")

    # ------------------------------------------------------------------
    # Phase checks
    # ------------------------------------------------------------------

    def check_files(self) -> int:
        self._section("Phase 0 – Required Files")
        max_pts = self.WEIGHTS["files"]

        components = {
            "client":                ["client.c", "client.cc", "client.cpp", "client.py"],
            "hospital_server":       ["hospital_server.c", "hospital_server.cc",
                                      "hospital_server.cpp", "hospital_server.py"],
            "authentication_server": ["authentication_server.c", "authentication_server.cc",
                                      "authentication_server.cpp", "authentication_server.py"],
            "appointment_server":    ["appointment_server.c", "appointment_server.cc",
                                      "appointment_server.cpp", "appointment_server.py"],
            "prescription_server":   ["prescription_server.c", "prescription_server.cc",
                                      "prescription_server.cpp", "prescription_server.py"],
        }

        makefile_ok = (self.submission_dir / "Makefile").exists()
        readme_ok = any(
            (self.submission_dir / r).exists()
            for r in ["README", "README.md", "readme.txt", "readme.md", "README.txt"]
        )

        if not makefile_ok:
            self._record("Makefile present (REQUIRED – will not grade without it)", 0, 0)
            return 0
        if not readme_ok:
            self._record("README present (REQUIRED – will not grade without it)", 0, 0)
            return 0

        missing = [
            comp for comp, variants in components.items()
            if not any((self.submission_dir / v).exists() for v in variants)
        ]

        if missing:
            self._record(f"Source files present (missing: {', '.join(missing)})", 0, max_pts)
            return 0

        self._record("All required files present (Makefile, README, source files)", max_pts, max_pts)
        return max_pts

    def check_compile(self) -> int:
        self._section("Phase 0 – Compilation")
        max_pts = self.WEIGHTS["compile"]

        try:
            result = subprocess.run(
                ["make", "all"],
                cwd=str(self.submission_dir),
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            self._record("make all (timed out after 120s)", 0, max_pts)
            return 0
        except FileNotFoundError:
            self._record("make all (make not found)", 0, max_pts)
            return 0

        if result.returncode == 0:
            self._record("make all succeeded", max_pts, max_pts)
            return max_pts
        else:
            if self.verbose:
                print(f"\n  Compiler output (last 20 lines):")
                for line in result.stderr.strip().splitlines()[-20:]:
                    print(f"    {line}")
            self._record("make all failed – receiving 5/100 total per spec", 0, max_pts)
            return 0

    def check_phase1a(self) -> int:
        """Boot-up messages for all four servers."""
        self._section("Phase 1A – Server Boot-Up Messages")
        max_pts = self.WEIGHTS["phase1a"]
        pts_per_server = max_pts // 4  # 2 pts each
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

        self._readers = {}  # name -> OutputReader

        # Start servers in spec-required order
        for name, expected_msg in boot_specs:
            cmd = self._find_executable(name)
            if not cmd:
                self._record(f"{name} boot-up message", 0, pts_per_server)
                continue

            proc = self._start_process(cmd, name)
            reader = OutputReader(proc, name)
            self._readers[name] = reader

            found = reader.wait_for(expected_msg, timeout=8.0)
            self._log(f"{name} boot output:\n{reader.all_output()}")

            if found:
                self._record(f"{name} boot-up message", pts_per_server, pts_per_server)
                total += pts_per_server
            else:
                self._record(
                    f"{name} boot-up message (expected: \"{expected_msg}\")", 0, pts_per_server
                )

        # Give servers a moment to fully initialize
        time.sleep(1.0)
        return total

    def _run_client_session(self, args: list, commands: list, timeout_per_cmd: float = 3.0) -> str:
        """
        Start the client with `args` (e.g. [username, password]),
        send each command, collect all output, then quit.
        Returns the full combined stdout of the client session.
        """
        cmd = self._find_executable("client")
        if not cmd:
            return ""
        full_cmd = cmd + list(args)
        proc = self._start_process(full_cmd, "client")
        reader = OutputReader(proc, "client")
        time.sleep(1.5)  # wait for auth to complete

        for command in commands:
            self._log(f"  -> sending: {command!r}")
            try:
                proc.stdin.write(command + "\n")
                proc.stdin.flush()
            except BrokenPipeError:
                break
            time.sleep(timeout_per_cmd)

        # Send quit
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

        output = reader.all_output()
        self._log(f"Client output:\n{output}")
        return output

    def check_phase1b(self) -> int:
        """Authentication: valid doctor, valid patient, invalid credentials."""
        self._section("Phase 1B – Authentication")
        max_pts = self.WEIGHTS["phase1b"]
        total = 0

        # --- Sub-test 1: Valid doctor login ---
        pts = 7
        out = self._run_client_session(
            [self.doctor_name, self.doctor_pass], [], timeout_per_cmd=1.0
        )
        expected_ok  = "Authentication successful"
        expected_doc = "doctor access"
        if expected_ok in out and expected_doc in out:
            self._record(f"Valid doctor login", pts, pts)
            total += pts
        else:
            self._record(
                f"Valid doctor login (expected auth success + doctor access grant)", 0, pts
            )

        # --- Sub-test 2: Valid patient login ---
        pts = 7
        out = self._run_client_session(
            [self.patient_name, self.patient_pass], [], timeout_per_cmd=1.0
        )
        expected_pat = "patient access"
        if expected_ok in out and expected_pat in out:
            self._record("Valid patient login", pts, pts)
            total += pts
        else:
            self._record(
                "Valid patient login (expected auth success + patient access grant)", 0, pts
            )

        # --- Sub-test 3: Invalid credentials ---
        pts = 6
        out = self._run_client_session(
            [self.bad_user, self.bad_pass], [], timeout_per_cmd=1.0
        )
        if "incorrect" in out.lower() or "failed" in out.lower() or "invalid" in out.lower():
            self._record("Invalid credentials rejected", pts, pts)
            total += pts
        else:
            self._record(
                "Invalid credentials rejected (expected failure/incorrect message)", 0, pts
            )

        return total

    def check_phase2(self) -> int:
        """Phase 2: patient and doctor commands."""
        self._section("Phase 2 – Patient & Doctor Commands")
        max_pts = self.WEIGHTS["phase2"]
        total = 0

        # ---- lookup (list doctors) ----
        pts = 5
        out = self._run_client_session(
            [self.patient_name, self.patient_pass],
            ["lookup"],
        )
        if self.doctor_name in out:
            self._record("lookup (list all doctors)", pts, pts)
            total += pts
        else:
            self._record("lookup (expected doctor list in response)", 0, pts)

        # ---- lookup <doctor> – all slots free ----
        pts = 5
        out = self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"lookup {self.doctor_name}"],
        )
        if ("all time blocks are available" in out.lower() or
                "09:00" in out or "10:00" in out):
            self._record(f"lookup {self.doctor_name} (availability shown)", pts, pts)
            total += pts
        else:
            self._record(
                f"lookup {self.doctor_name} (expected availability info)", 0, pts
            )

        # ---- schedule: successful booking ----
        pts = 7
        out = self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"],
        )
        # Reset appointments file so later tests start fresh
        self._reset_appointments()
        if "successfully" in out.lower() or "scheduled" in out.lower():
            self._record(
                f"schedule {self.doctor_name} {self.test_time} {self.test_illness} (success)", pts, pts
            )
            total += pts
        else:
            self._record(
                f"schedule (expected success message)", 0, pts
            )

        # ---- schedule: slot already taken ----
        pts = 4
        # Book the slot first, then try again
        self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"],
        )
        out = self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"],
        )
        self._reset_appointments()
        if ("not available" in out.lower() or "unable" in out.lower() or
                "taken" in out.lower() or "failed" in out.lower()):
            self._record("schedule (duplicate slot correctly rejected)", pts, pts)
            total += pts
        else:
            self._record("schedule (duplicate slot – expected failure message)", 0, pts)

        # ---- view_appointment ----
        pts = 4
        # Book first, then view
        self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"],
        )
        out = self._run_client_session(
            [self.patient_name, self.patient_pass],
            ["view_appointment"],
        )
        self._reset_appointments()
        if self.doctor_name in out or self.test_time in out:
            self._record("view_appointment (appointment details returned)", pts, pts)
            total += pts
        else:
            self._record("view_appointment (expected appointment details)", 0, pts)

        # ---- cancel ----
        pts = 5
        # Book, then cancel
        self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"],
        )
        out = self._run_client_session(
            [self.patient_name, self.patient_pass],
            ["cancel"],
        )
        self._reset_appointments()
        if "successfully cancelled" in out.lower() or "cancelled" in out.lower():
            self._record("cancel (cancellation successful)", pts, pts)
            total += pts
        else:
            self._record("cancel (expected cancellation success message)", 0, pts)

        # ---- Doctor: view_appointments ----
        pts = 5
        # Book a patient, then doctor views
        self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"],
        )
        out = self._run_client_session(
            [self.doctor_name, self.doctor_pass],
            ["view_appointments"],
        )
        self._reset_appointments()
        if self.test_time in out or self.patient_hash_suffix in out:
            self._record("view_appointments (doctor sees booked slot)", pts, pts)
            total += pts
        else:
            self._record("view_appointments (expected scheduled slots in response)", 0, pts)

        return total

    def check_phase3(self) -> int:
        """Phase 3: prescription commands."""
        self._section("Phase 3 – Prescription Commands")
        max_pts = self.WEIGHTS["phase3"]
        total = 0

        # Book a patient appointment (prerequisite for prescribe)
        self._run_client_session(
            [self.patient_name, self.patient_pass],
            [f"schedule {self.doctor_name} {self.test_time} {self.test_illness}"],
        )

        # ---- prescribe ----
        pts = 10
        out = self._run_client_session(
            [self.doctor_name, self.doctor_pass],
            [f"prescribe {self.patient_name} Daily"],
        )
        if ("successfully prescribed" in out.lower() or
                self.test_treatment.lower() in out.lower()):
            self._record(
                f"prescribe {self.patient_name} Daily (prescription saved)", pts, pts
            )
            total += pts
        else:
            self._record(
                f"prescribe (expected success + treatment name in response)", 0, pts
            )

        # ---- view_prescription (doctor) ----
        pts = 7
        out = self._run_client_session(
            [self.doctor_name, self.doctor_pass],
            [f"view_prescription {self.patient_name}"],
        )
        if (self.test_treatment.lower() in out.lower() or
                "Daily" in out or "daily" in out.lower()):
            self._record(
                f"view_prescription {self.patient_name} (doctor sees prescription)", pts, pts
            )
            total += pts
        else:
            self._record(
                f"view_prescription (doctor) – expected treatment/frequency in response", 0, pts
            )

        # ---- view_prescription (patient) ----
        pts = 8
        out = self._run_client_session(
            [self.patient_name, self.patient_pass],
            ["view_prescription"],
        )
        self._reset_prescriptions()
        self._reset_appointments()
        if (self.test_treatment.lower() in out.lower() or
                self.doctor_name in out or "daily" in out.lower()):
            self._record(
                "view_prescription (patient sees their prescription)", pts, pts
            )
            total += pts
        else:
            self._record(
                "view_prescription (patient) – expected treatment/doctor in response", 0, pts
            )

        return total

    # ------------------------------------------------------------------
    # Reset helpers (restore data files between test cases)
    # ------------------------------------------------------------------

    def _reset_appointments(self):
        """Restore appointments.txt to all-empty slots (no patients booked)."""
        with open(self.submission_dir / "appointments.txt", "w") as f:
            f.write(f"{self.doctor_name}\n")
            for hour in range(9, 17):
                f.write(f"{hour:02d}:00\n")
        time.sleep(0.3)

    def _reset_prescriptions(self):
        """Clear prescriptions.txt."""
        with open(self.submission_dir / "prescriptions.txt", "w") as f:
            f.write("")
        time.sleep(0.3)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> int:
        print(f"\n{'#'*60}")
        print(f"  EE450 Socket Programming Autograder (Spring 2026)")
        print(f"  Submission : {self.submission_dir}")
        print(f"  USC suffix : {self.usc_suffix}")
        print(f"  Ports      : auth={self.auth_udp_port}, appt={self.appt_udp_port},")
        print(f"               presc={self.presc_udp_port}, hosp_udp={self.hosp_udp_port},")
        print(f"               hosp_tcp={self.hosp_tcp_port}")
        print(f"{'#'*60}\n")

        try:
            # --- File & compilation checks ---
            file_pts = self.check_files()
            if file_pts == 0:
                print("\n  *** Submission missing Makefile or README – CANNOT GRADE ***")
                self._print_summary()
                return self.score

            compile_pts = self.check_compile()
            if compile_pts == 0:
                self.score = 5  # spec: 5/100 for non-compiling code
                print("\n  *** Compilation failed – grading stops (5/100 per spec) ***")
                self._print_summary()
                return self.score

            # --- Create test data ---
            self._create_test_data()

            # --- Start servers & check boot-up ---
            p1a_pts = self.check_phase1a()

            # --- Run functional tests ---
            p1b_pts = self.check_phase1b()
            p2_pts  = self.check_phase2()
            p3_pts  = self.check_phase3()

        finally:
            self._kill_all()

        self._print_summary()
        return self.score

    def _print_summary(self):
        print(f"\n{'='*60}")
        print("  GRADING SUMMARY")
        print(f"{'='*60}")
        for line in self.feedback:
            print(line)
        print(f"\n  {'─'*40}")
        print(f"  TOTAL SCORE: {self.score} / 100")
        print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="EE450 Spring 2026 Socket Programming Project Autograder"
    )
    parser.add_argument(
        "submission_dir",
        help="Path to the extracted submission directory",
    )
    parser.add_argument(
        "--usc-id",
        dest="usc_id",
        default="000",
        help="Last 3 digits of student's USC ID (used to derive port numbers). Default: 000",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print debug information including raw process output",
    )
    args = parser.parse_args()

    grader = Grader(
        submission_dir=args.submission_dir,
        usc_suffix=args.usc_id,
        verbose=args.verbose,
    )
    sys.exit(grader.run())


if __name__ == "__main__":
    main()
