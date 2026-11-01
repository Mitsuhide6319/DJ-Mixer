# DJ Mixer の起動用ショートカット（アイコンつき）を作る。
#   powershell -ExecutionPolicy Bypass -File tools\make_shortcut.ps1            # デスクトップとこのフォルダに作る
#   powershell -ExecutionPolicy Bypass -File tools\make_shortcut.ps1 -NoDesktop # このフォルダにだけ作る
# ショートカットには、起動したウィンドウと同じ AppUserModelID（DJMixer.App）を付ける。
# こうすると、タスクバーにピン留めしたアイコンと起動中のウィンドウが 1 つにまとまる。
# タスクバーにピン留め済みの「DJ Mixer」があれば、それにも同じ ID を付ける。
param([switch]$NoDesktop)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$appId = 'DJMixer.App'

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class AumidLink {
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PROPERTYKEY { public Guid fmtid; public uint pid; }
    [StructLayout(LayoutKind.Explicit, Size = 24)]
    public struct PROPVARIANT { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr p; }
    [ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPropertyStore {
        int GetCount(out uint c); int GetAt(uint i, out PROPERTYKEY k); int GetValue(ref PROPERTYKEY k, out PROPVARIANT v);
        int SetValue(ref PROPERTYKEY k, ref PROPVARIANT v); int Commit();
    }
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHGetPropertyStoreFromParsingName(string path, IntPtr bc, int flags, ref Guid iid, out IPropertyStore ps);
    public static void Set(string lnk, string id) {
        Guid iid = typeof(IPropertyStore).GUID; IPropertyStore ps;
        SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 2 /* GPS_READWRITE */, ref iid, out ps);
        var key = new PROPERTYKEY { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };
        var v = new PROPVARIANT { vt = 31 /* VT_LPWSTR */, p = Marshal.StringToCoTaskMemUni(id) };
        try { Marshal.ThrowExceptionForHR(ps.SetValue(ref key, ref v)); Marshal.ThrowExceptionForHR(ps.Commit()); }
        finally { Marshal.FreeCoTaskMem(v.p); Marshal.ReleaseComObject(ps); }
    }
}
'@

$targets = @(Join-Path $root 'DJ Mixer.lnk')
if (-not $NoDesktop) { $targets += Join-Path ([Environment]::GetFolderPath('Desktop')) 'DJ Mixer.lnk' }
$sh = New-Object -ComObject WScript.Shell
foreach ($t in $targets) {
  $s = $sh.CreateShortcut($t)
  $s.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
  $s.Arguments = '"' + (Join-Path $root 'DJ Mixer.vbs') + '"'
  $s.WorkingDirectory = $root
  $s.IconLocation = (Join-Path $root 'assets\icon.ico') + ',0'
  $s.Description = 'DJ Mixer（コンソールの窓を出さずに起動）'
  $s.Save()
  [AumidLink]::Set($t, $appId)
  Write-Output "作りました: $t"
}
# タスクバーにピン留め済みのショートカットにも同じ ID（起動中のウィンドウとまとまるように）
$pinned = Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar\DJ Mixer.lnk'
if (Test-Path $pinned) { [AumidLink]::Set($pinned, $appId); Write-Output "ピン留めにも ID を付けました: $pinned" }
