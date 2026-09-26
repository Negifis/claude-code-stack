param([Parameter(Mandatory)][string[]]$Path)
# Names the processes holding each file open, through the built-in Restart Manager API
# (read-only: no handle is opened on the file and nothing is signalled). handle.exe is not
# installed on this machine and Get-CimInstance cannot see file handles.
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class RmFileHolders {
    [StructLayout(LayoutKind.Sequential)]
    struct RM_UNIQUE_PROCESS { public int dwProcessId; public System.Runtime.InteropServices.ComTypes.FILETIME ProcessStartTime; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct RM_PROCESS_INFO {
        public RM_UNIQUE_PROCESS Process;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string strAppName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string strServiceShortName;
        public int ApplicationType; public uint AppStatus; public uint TSSessionId;
        [MarshalAs(UnmanagedType.Bool)] public bool bRestartable;
    }
    const int ERROR_MORE_DATA = 234;
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)] static extern int RmStartSession(out uint handle, int flags, string key);
    [DllImport("rstrtmgr.dll")] static extern int RmEndSession(uint handle);
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)] static extern int RmRegisterResources(uint handle, uint nFiles, string[] files, uint nApps, RM_UNIQUE_PROCESS[] apps, uint nServices, string[] services);
    [DllImport("rstrtmgr.dll")] static extern int RmGetList(uint handle, out uint needed, ref uint count, [In, Out] RM_PROCESS_INFO[] info, ref uint reasons);
    public static int[] Of(string path) {
        uint handle;
        int rc = RmStartSession(out handle, 0, Guid.NewGuid().ToString());
        if (rc != 0) throw new Exception("RmStartSession failed: " + rc);
        try {
            rc = RmRegisterResources(handle, 1, new[] { path }, 0, null, 0, null);
            if (rc != 0) throw new Exception("RmRegisterResources failed: " + rc);
            uint needed = 0, count = 0, reasons = 0;
            rc = RmGetList(handle, out needed, ref count, null, ref reasons);
            if (rc == 0) return new int[0];
            if (rc != ERROR_MORE_DATA) throw new Exception("RmGetList failed: " + rc);
            var info = new RM_PROCESS_INFO[needed];
            count = needed;
            rc = RmGetList(handle, out needed, ref count, info, ref reasons);
            if (rc != 0) throw new Exception("RmGetList failed: " + rc);
            var ids = new List<int>();
            for (int i = 0; i < count; i++) ids.Add(info[i].Process.dwProcessId);
            return ids.ToArray();
        } finally { RmEndSession(handle); }
    }
}
'@
foreach ($p in $Path) {
    $ids = [RmFileHolders]::Of((Resolve-Path -LiteralPath $p).Path)
    "== $p : $($ids.Count) holder(s)"
    foreach ($id in $ids) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$id"
        '  pid={0} parent={1} started={2} exe={3}' -f $id, $proc.ParentProcessId, $proc.CreationDate, $proc.ExecutablePath
    }
}
