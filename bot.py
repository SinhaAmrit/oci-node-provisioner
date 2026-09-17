#!/usr/bin/env python3
"""
OCI Ampere A1 Provisioner — Final
Target: Canonical Ubuntu 24.04 aarch64
- Random 85-95s interval (proven zero-429 sweet spot)
- Telegram notification on start + success + fatal errors
- Auto-disables the GitHub workflow after instance creation
"""

import os
import sys
import time
import json
import random
import urllib.request
import oci
from datetime import datetime, timezone

# ─── CONFIG FROM GITHUB SECRETS ────────────────────────────────────
OCI_TENANCY_ID     = os.environ["OCI_TENANCY_ID"]
OCI_USER_ID        = os.environ["OCI_USER_ID"]
OCI_REGION         = os.environ["OCI_REGION"]
OCI_FINGERPRINT    = os.environ["OCI_FINGERPRINT"]
OCI_PRIVATE_KEY    = os.environ["OCI_PRIVATE_KEY"]
OCI_SUBNET_ID      = os.environ["OCI_SUBNET_ID"]
OCI_IMAGE_ID       = os.environ["OCI_IMAGE_ID"]
OCI_PUBLIC_SSH_KEY = os.environ["OCI_PUBLIC_SSH_KEY"]
OCI_COMPARTMENT_ID = os.environ.get("OCI_STACK_ID", OCI_TENANCY_ID)

# Telegram (optional — secrets missing ho toh silently skip)
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
TELEGRAM_UID = os.environ.get("TELEGRAM_UID", "")

# GitHub auto-disable (github.token from workflow)
GH_REPO       = os.environ.get("GITHUB_REPOSITORY", "")  # Actions auto-set karta hai
GH_TOKEN      = os.environ.get("GH_TOKEN", "")
WORKFLOW_PATH = os.environ.get("WORKFLOW_PATH", ".github/workflows/oci_spawn.yml")

# Instance config
INSTANCE_NAME = "ampere-ubuntu2404"
SHAPE         = "VM.Standard.A1.Flex"
OCPUS         = int(os.environ.get("OCPUS", "2"))
MEMORY_GB     = int(os.environ.get("MEMORY_GB", "12"))
BOOT_VOLUME_GB = int(os.environ.get("BOOT_VOLUME_GB", "150"))

# Retry config — proven sweet spot (zero 429 zone)
MAX_ATTEMPTS  = int(os.environ.get("MAX_ATTEMPTS", "18"))
WAIT_MIN      = int(os.environ.get("WAIT_MIN", "85"))
WAIT_MAX      = int(os.environ.get("WAIT_MAX", "95"))

# 429 insurance (normally trigger nahi hoga)
COOLDOWN_BASE = int(os.environ.get("COOLDOWN_BASE", "240"))
COOLDOWN_MAX  = int(os.environ.get("COOLDOWN_MAX", "1200"))

# ─── OCI CLIENT SETUP ──────────────────────────────────────────────
config = {
    "tenancy":     OCI_TENANCY_ID,
    "user":        OCI_USER_ID,
    "region":      OCI_REGION,
    "fingerprint": OCI_FINGERPRINT,
    "key_content": OCI_PRIVATE_KEY.replace("\\n", "\n"),
}

compute_client  = oci.core.ComputeClient(config)
identity_client = oci.identity.IdentityClient(config)
network_client  = oci.core.VirtualNetworkClient(config)


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def tg_send(msg):
    """Telegram notification — fail hone par bhi main script nahi rukegi."""
    if not BOT_TOKEN or not TELEGRAM_UID:
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = json.dumps({"chat_id": TELEGRAM_UID, "text": msg}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        log("📲 Telegram notification sent.")
    except Exception as e:
        log(f"⚠️  Telegram send failed: {e}")


def disable_workflow():
    """GitHub API se workflow disable (success ke baad cron auto-band)."""
    if not GH_REPO or not GH_TOKEN:
        log("⚠️  GH_TOKEN missing — workflow auto-disable skip. Manual disable karo.")
        return
    try:
        url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{WORKFLOW_PATH}/disable"
        req = urllib.request.Request(url, data=b"", method="PUT", headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
        })
        resp = urllib.request.urlopen(req, timeout=10)
        if resp.status in (200, 204):
            log("✅ Workflow auto-disabled. Cron ab nahi chalega.")
            tg_send("🔒 Workflow auto-disabled. No more cron runs.")
        else:
            log(f"⚠️  Disable API returned {resp.status} — manual disable karo.")
    except Exception as e:
        log(f"⚠️  Auto-disable failed: {e} — manual disable kar lena.")


def get_availability_domain():
    """Free tier home regions have a single AD — use the first one."""
    ads = identity_client.list_availability_domains(
        compartment_id=OCI_COMPARTMENT_ID
    ).data
    return ads[0].name


def build_launch_details(ad_name):
    """Construct the LaunchInstanceDetails payload."""
    return oci.core.models.LaunchInstanceDetails(
        compartment_id=OCI_COMPARTMENT_ID,
        availability_domain=ad_name,
        display_name=INSTANCE_NAME,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=OCPUS,
            memory_in_gbs=MEMORY_GB,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=OCI_IMAGE_ID,
            boot_volume_size_in_gbs=BOOT_VOLUME_GB,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=OCI_SUBNET_ID,
            assign_public_ip=True,
            display_name=f"{INSTANCE_NAME}-vnic",
        ),
        metadata={"ssh_authorized_keys": OCI_PUBLIC_SSH_KEY},
        agent_config=oci.core.models.LaunchInstanceAgentConfigDetails(
            is_monitoring_disabled=False,
            is_management_disabled=False,
        ),
    )


def get_active_instances():
    """Check if an instance with same name already exists (avoid duplicates)."""
    try:
        instances = compute_client.list_instances(
            compartment_id=OCI_COMPARTMENT_ID,
            display_name=INSTANCE_NAME,
        ).data
        return [i for i in instances if i.lifecycle_state in (
            "RUNNING", "STARTING", "PROVISIONING"
        )]
    except Exception as e:
        log(f"⚠️  Could not list instances: {e}")
        return []


def attempt_launch(ad_name):
    """Single launch attempt. Returns (instance, error)."""
    try:
        response = compute_client.launch_instance(build_launch_details(ad_name))
        return response.data, None
    except oci.exceptions.ServiceError as e:
        return None, e
    except Exception as e:
        log(f"⚠️  Unexpected error: {type(e).__name__}: {e}")
        return None, None


def wait_for_running(instance_id, timeout=600):
    """Poll until instance reaches RUNNING state."""
    log("⏳ Waiting for instance to enter RUNNING state...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            inst = compute_client.get_instance(instance_id).data
            log(f"   State: {inst.lifecycle_state}")
            if inst.lifecycle_state == "RUNNING":
                return True
            if inst.lifecycle_state in ("TERMINATED", "FAILED"):
                return False
        except Exception as e:
            log(f"   Poll error: {e}")
        time.sleep(15)
    log("⏰ Timeout waiting for RUNNING state.")
    return False


def get_instance_ip(instance_id):
    """Fetch public IP of the launched instance."""
    try:
        vnic_attachments = compute_client.list_vnic_attachments(
            compartment_id=OCI_COMPARTMENT_ID,
            instance_id=instance_id,
        ).data
        for va in vnic_attachments:
            vnic = network_client.get_vnic(va.vnic_id).data
            if vnic.public_ip:
                log(f"🌐 Public IP: {vnic.public_ip}")
                log(f"🔐 SSH: ssh ubuntu@{vnic.public_ip}")
                return vnic.public_ip
    except Exception as e:
        log(f"⚠️  Could not fetch IP: {e}")
    return None


def main():
    log("=" * 60)
    log("OCI Ampere Provisioner — Ubuntu 24.04 aarch64")
    log("=" * 60)
    log(f"Region:        {OCI_REGION}")
    log(f"Shape:         {SHAPE} ({OCPUS} OCPU / {MEMORY_GB} GB)")
    log(f"Boot Volume:   {BOOT_VOLUME_GB} GB")
    log(f"Max Attempts:  {MAX_ATTEMPTS}")
    log(f"Random wait:   {WAIT_MIN}s — {WAIT_MAX}s")
    log("")

    tg_send(
        f"🤖 Provisioner started\n"
        f"Region: {OCI_REGION}\n"
        f"Shape: {OCPUS} OCPU / {MEMORY_GB} GB\n"
        f"Attempts: {MAX_ATTEMPTS} ({WAIT_MIN}-{WAIT_MAX}s)"
    )

    # Duplicate check
    existing = get_active_instances()
    if existing:
        log(f"⚠️  Instance already exists: {existing[0].id}")
        log("Skipping to avoid duplicates.")
        tg_send("ℹ️  Instance already exists — no new launch needed.")
        return 0

    ad_name = get_availability_domain()
    log(f"Availability Domain: {ad_name}")
    log("")

    consecutive_429 = 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        log(f"── Attempt {attempt}/{MAX_ATTEMPTS} ──")
        instance, error = attempt_launch(ad_name)

        if instance is not None:
            log(f"✅ Launched: {instance.id}")
            if wait_for_running(instance.id):
                ip = get_instance_ip(instance.id)
                log("🚀 Instance is live!")
                # ─── SUCCESS: Notify + Auto-stop ───
                tg_send(
                    f"🎉 VPS CREATED!\n\n"
                    f"🖥  Name: {INSTANCE_NAME}\n"
                    f"📍 Region: {OCI_REGION}\n"
                    f"⚙️  Shape: {SHAPE} ({OCPUS} OCPU / {MEMORY_GB} GB)\n"
                    f"💾 Boot: {BOOT_VOLUME_GB} GB\n"
                    f"🌐 IP: {ip or 'check console'}\n\n"
                    f"🔐 SSH: ssh ubuntu@{ip or '<IP>'}"
                )
                disable_workflow()
                return 0
            return 1

        if error is not None:
            if error.status == 500 and "Out of host capacity" in error.message:
                log("⏳ Out of host capacity.")
                consecutive_429 = 0
            elif error.status == 429:
                consecutive_429 += 1
                cooldown = min(COOLDOWN_BASE * consecutive_429, COOLDOWN_MAX)
                log(f"⏳ Rate limited (429 x{consecutive_429}). Cooling down {cooldown}s...")
                time.sleep(cooldown)
            elif error.status == 401:
                log("❌ Auth failed. Check credentials.")
                tg_send("❌ Provisioner STOPPED: Auth failed (401). Check secrets!")
                return 1
            elif error.status == 404:
                log(f"❌ Not found: {error.message}")
                tg_send(f"❌ Provisioner STOPPED: Not found (404): {error.message}")
                return 1
            else:
                log(f"⚠️  Error {error.status}: {error.message}")

        if attempt < MAX_ATTEMPTS:
            wait = random.randint(WAIT_MIN, WAIT_MAX)
            log(f"😴 Sleeping {wait}s (random)...")
            time.sleep(wait)

    log("❌ Max attempts reached. Exiting.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
