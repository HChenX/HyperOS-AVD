import os
import zipfile
import hashlib

def build():
    workspace = r"D:\Android\HyperOS-AVD"
    work_dir = os.path.join(workspace, "work")
    so_path = os.path.join(work_dir, "mapper.ranchu.patched.so") if os.path.isfile(os.path.join(work_dir, "mapper.ranchu.patched.so")) else os.path.join(workspace, "mapper.ranchu.patched.so")
    with open(so_path, "rb") as f:
        so_data = f.read()

    sha = hashlib.sha256(so_data).hexdigest()
    print(f"mapper.ranchu.patched.so SHA256: {sha}, Size: {len(so_data)}")
    assert sha.lower() == "ff352920af868ed20ef097090c38a1c3783dc7f837fca3aea77102b40606a497"

    module_prop = """id=ranchu_mapper_fix
name=Ranchu Mapper Fix
version=v1.0.0
versionCode=100
author=HyperOS-AVD
description=Replaces /vendor/lib64/hw/mapper.ranchu.so with host memory allocator fix via Hybrid-Mount
"""

    customize_sh = """#!/system/bin/sh
ui_print "- Installing Ranchu Mapper Fix Module"
ui_print "- Target: /vendor/lib64/hw/mapper.ranchu.so"

chmod -R 755 "$MODPATH"
chmod 644 "$MODPATH/module.prop"

if [ -f "$MODPATH/system/vendor/lib64/hw/mapper.ranchu.so" ]; then
    chown 0:2000 "$MODPATH/system/vendor/lib64/hw/mapper.ranchu.so"
    chmod 0755 "$MODPATH/system/vendor/lib64/hw/mapper.ranchu.so"
    chcon u:object_r:vendor_file:s0 "$MODPATH/system/vendor/lib64/hw/mapper.ranchu.so" 2>/dev/null || true
fi

if [ -f "$MODPATH/vendor/lib64/hw/mapper.ranchu.so" ]; then
    chown 0:2000 "$MODPATH/vendor/lib64/hw/mapper.ranchu.so"
    chmod 0755 "$MODPATH/vendor/lib64/hw/mapper.ranchu.so"
    chcon u:object_r:vendor_file:s0 "$MODPATH/vendor/lib64/hw/mapper.ranchu.so" 2>/dev/null || true
fi

ui_print "- Ranchu Mapper Fix installed successfully"
"""

    service_sh = """#!/system/bin/sh
# Background service to ensure stability
sleep 5
setprop ctl.stop hypsys_system 2>/dev/null || true
setprop ctl.stop misight 2>/dev/null || true
"""

    post_fs_data_sh = """#!/system/bin/sh
setprop ctl.stop hypsys_system 2>/dev/null || true
setprop ctl.stop misight 2>/dev/null || true
"""

    zip_out = os.path.join(work_dir, "ranchu_mapper_fix.zip")
    with zipfile.ZipFile(zip_out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("module.prop", module_prop.replace("\r\n", "\n"))
        z.writestr("customize.sh", customize_sh.replace("\r\n", "\n"))
        z.writestr("service.sh", service_sh.replace("\r\n", "\n"))
        z.writestr("post-fs-data.sh", post_fs_data_sh.replace("\r\n", "\n"))
        z.writestr("system/vendor/lib64/hw/mapper.ranchu.so", so_data)
        z.writestr("vendor/lib64/hw/mapper.ranchu.so", so_data)

    print(f"Successfully generated {zip_out}")
    with zipfile.ZipFile(zip_out, "r") as z:
        print("Zip contents:", z.namelist())

if __name__ == "__main__":
    build()
