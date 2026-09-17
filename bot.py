
#!/usr/bin/env python3
"""
OCI Ampere A1 Provisioner
Target: Canonical Ubuntu 22.04 Minimal aarch64
Runs via GitHub Actions, uses env vars from repo secrets.
"""

import os
import sys
import time
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

# Instance config
INSTANCE_NAME = "ampere-ubuntu2204"
SHAPE         = "VM.Standard.A1.Flex"
OCPUS         = int(os.environ.get("OCPUS", "2"))
MEMORY_GB     = int(os.environ.get("MEMORY_GB", "12"))
BOOT_VOLUME_GB = int(os.environ.get("BOOT_VOLUME_GB", "150"))
AD_INDEX      = os.environ.get("OCI_AD", "1")  # 1, 2, or 3

# Retry config
MAX_ATTEMPTS  = int(os.environ.get("MAX_ATTEMPTS", "30"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))

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


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def get_availability_domain():
    """Resolve AD name based on region + index."""
    ads = identity_client.list_availability_domains(
        compartment_id=OCI_COMPARTMENT_ID
    ).data
    target = f"{OCI_REGION}-AD-{AD_INDEX}"
    for ad in ads:
        if ad.name == target:
            return ad.name
    log(f"⚠️  {target} not found, using {ads[0].name}")
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
            # Use network client to get VNIC details
            from oci.core import VirtualNetworkClient
            network_client = VirtualNetworkClient(config)
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
    log("OCI Ampere Provisioner — Ubuntu 22.04 Minimal aarch64")
    log("=" * 60)
    log(f"Region:        {OCI_REGION}")
    log(f"Shape:         {SHAPE} ({OCPUS} OCPU / {MEMORY_GB} GB)")
    log(f"Boot Volume:   {BOOT_VOLUME_GB} GB")
    log(f"Max Attempts:  {MAX_ATTEMPTS}")
    log(f"Interval:      {POLL_INTERVAL}s")
    log("")

    # Duplicate check
    existing = get_active_instances()
    if existing:
        log(f"⚠️  Instance already exists: {existing[0].id}")
        log("Skipping to avoid duplicates.")
        return 0

    ad_name = get_availability_domain()
    log(f"Availability Domain: {ad_name}")
    log("")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        log(f"── Attempt {attempt}/{MAX_ATTEMPTS} ──")
        instance, error = attempt_launch(ad_name)

        if instance is not None:
            log(f"✅ Launched: {instance.id}")
            if wait_for_running(instance.id):
                get_instance_ip(instance.id)
                log("🚀 Instance is live!")
                return 0
            return 1

        if error is not None:
            if error.status == 500 and "Out of host capacity" in error.message:
                log("⏳ Out of host capacity.")
            elif error.status == 429:
                log("⏳ Rate limited. Extra 150s wait...")
                time.sleep(150)  # Extra wait on top of normal POLL_INTERVAL
            elif error.status == 401:
                log("❌ Auth failed. Check credentials.")
                return 1
            elif error.status == 404:
                log(f"❌ Not found: {error.message}")
                return 1
            else:
                log(f"⚠️  Error {error.status}: {error.message}")

        if attempt < MAX_ATTEMPTS:
            log(f"😴 Sleeping {POLL_INTERVAL}s...")
            time.sleep(POLL_INTERVAL)

    log("❌ Max attempts reached. Exiting.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
