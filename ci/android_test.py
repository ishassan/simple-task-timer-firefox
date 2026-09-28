#!/usr/bin/env python3
"""Load the add-on into Firefox for Android on an emulator and take screenshots.

Run by .github/workflows/android-test.yml after the emulator has started.
The screenshots are for a person to look at. The script only fails when the
add-on could not be loaded at all; other steps are best effort, and each one
is reported as OK or MISSED at the end.
"""
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

PKG = 'org.mozilla.firefox'
DEVICE = 'emulator-5554'
APK = 'firefox.apk'
OUT = 'ci-out'

results = []
shot_no = 0


def adb(*args, check=True):
    return subprocess.run(['adb', '-s', DEVICE, *args], check=check,
                          capture_output=True, text=True).stdout


def ui_xml():
    # uiautomator sometimes fails while the screen is changing, so try again.
    for _ in range(3):
        adb('shell', 'uiautomator', 'dump', '/sdcard/ui.xml', check=False)
        xml = adb('exec-out', 'cat', '/sdcard/ui.xml', check=False)
        if xml.startswith('<?xml'):
            return xml
        time.sleep(1)
    return ''


def shot(name):
    """Save a screenshot and the screen's UI tree (the tree helps fix this script)."""
    global shot_no
    shot_no += 1
    base = os.path.join(OUT, f'{shot_no:02d}-{name}')
    with open(base + '.png', 'wb') as f:
        f.write(subprocess.run(['adb', '-s', DEVICE, 'exec-out', 'screencap', '-p'],
                               capture_output=True).stdout)
    with open(base + '.xml', 'w') as f:
        f.write(ui_xml())
    print(f'screenshot: {base}.png')


def find(pattern):
    """Return the center of the first element whose text, description or id matches."""
    xml = ui_xml()
    if not xml:
        return None
    rx = re.compile(pattern, re.I)
    for node in ET.fromstring(xml).iter('node'):
        for key in ('text', 'content-desc', 'resource-id'):
            if rx.search(node.get(key, '')):
                x1, y1, x2, y2 = map(int, re.findall(r'\d+', node.get('bounds', '')))
                return (x1 + x2) // 2, (y1 + y2) // 2
    return None


def tap(pattern, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        pos = find(pattern)
        if pos:
            adb('shell', 'input', 'tap', str(pos[0]), str(pos[1]))
            time.sleep(1.5)
            return True
        time.sleep(1)
    print(f'not found: {pattern}')
    return False


def scroll_down():
    adb('shell', 'input', 'swipe', '540', '1800', '540', '700', '300')
    time.sleep(1)


def back():
    adb('shell', 'input', 'keyevent', 'KEYCODE_BACK')
    time.sleep(1)


def record(step, ok):
    results.append((step, ok))
    print(f'{"OK" if ok else "MISSED"}: {step}')


def open_menu():
    return tap(r'^(Main menu|More options|Menu)$')


def skip_first_run_screens():
    """Close the welcome screens and permission prompts until the home screen shows."""
    for _ in range(15):
        if find(r'Search or enter address|mozac_browser_toolbar_url_view|toolbar_wrapper'):
            return True
        if not tap(r"^(Not now|Not Now|Skip|Maybe later|No thanks|Continue|Got it|"
                   r"Close|Dismiss|Start browsing|Allow|Don.t allow)$", timeout=3):
            time.sleep(2)
    return False


def enable_remote_debugging():
    """web-ext needs 'Remote debugging via USB' turned on in Firefox settings."""
    if not (open_menu() and tap(r'^Settings$')):
        return False
    for _ in range(12):
        if find(r'Remote debugging via USB'):
            tap(r'Remote debugging via USB')
            shot('remote-debugging-setting')
            back()
            return True
        scroll_down()
    return False


def main():
    os.makedirs(OUT, exist_ok=True)
    adb('wait-for-device')
    adb('install', '-r', APK)
    adb('shell', 'monkey', '-p', PKG, '-c', 'android.intent.category.LAUNCHER', '1')
    time.sleep(10)
    shot('firefox-first-start')

    record('Closed the welcome screens', skip_first_run_screens())
    shot('firefox-home')
    record('Turned on remote debugging', enable_remote_debugging())
    shot('back-from-settings')

    log_path = os.path.join(OUT, 'web-ext.log')
    log = open(log_path, 'w')
    webext = subprocess.Popen(
        ['web-ext', 'run', '--target', 'firefox-android', '--adb-device', DEVICE,
         '--firefox-apk', PKG, '--source-dir', 'src', '--no-input', '--verbose'],
        stdout=log, stderr=subprocess.STDOUT)
    loaded = False
    end = time.time() + 240
    while time.time() < end and webext.poll() is None:
        with open(log_path) as f:
            if re.search(r'Installed .* as a temporary add-on', f.read()):
                loaded = True
                break
        time.sleep(3)
    record('Loaded the add-on into Firefox for Android', loaded)
    if not loaded:
        webext.kill()
        shot('add-on-not-loaded')
        adb('logcat', '-d', check=False)
        return finish()

    # On install, the add-on opens its "installed" page in a new tab.
    time.sleep(8)
    shot('after-install')
    record('Opened the "installed" page by itself',
           bool(find(r'Task Timer|installed')))

    # Open the main page the way a user would: menu, Extensions, Task Timer.
    opened = open_menu() and tap(r'^(Extensions|Add-ons)$') and tap(r'Task Timer')
    time.sleep(4)
    shot('main-page')
    record('Opened the main page from the Firefox menu', opened)

    # Add a task and start it.
    added = tap(r'Task Name')
    if added:
        adb('shell', 'input', 'text', 'Android%stest%stask')
        added = tap(r'^Add Task$')
    time.sleep(2)
    shot('task-added')
    record('Added a task', added and bool(find(r'Android test task')))
    time.sleep(5)
    shot('five-seconds-later')

    webext.kill()
    return finish()


def finish():
    with open(os.path.join(OUT, 'logcat.txt'), 'w') as f:
        f.write(adb('logcat', '-d', check=False))
    lines = ['| Step | Result |', '|---|---|']
    lines += [f'| {step} | {"OK" if ok else "MISSED"} |' for step, ok in results]
    lines.append('\nScreenshots are in the **android-screenshots** artifact.')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as f:
            f.write('\n'.join(lines) + '\n')
    print('\n'.join(lines))
    loaded = any(ok for step, ok in results if step.startswith('Loaded'))
    return 0 if loaded else 1


if __name__ == '__main__':
    sys.exit(main())
