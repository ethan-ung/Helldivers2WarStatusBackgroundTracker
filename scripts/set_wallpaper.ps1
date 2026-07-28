<#
    IDesktopWallpaper shim.

    Windows exposes per-monitor wallpapers only through the IDesktopWallpaper COM
    interface; SystemParametersInfo can set one image for the whole desktop.
    This script wraps the interface so the Python side can list monitors and
    assign a distinct image to each.

    Modes:
      List                     -> JSON array of monitors (index, id, rect)
      Set     -Payload <json>  -> assign images: [{ "index": 0, "path": "..." }]
      Restore -Path <file>     -> set every monitor back to one image
#>
[CmdletBinding()]
param(
    [ValidateSet('List', 'Set', 'Restore')]
    [string]$Mode = 'List',

    [string]$Payload,
    [string]$Path,

    # DESKTOP_WALLPAPER_POSITION: Center 0, Tile 1, Stretch 2, Fit 3, Fill 4, Span 5
    [int]$Position = 4
)

$ErrorActionPreference = 'Stop'

$source = @'
using System;
using System.Runtime.InteropServices;

namespace HD2Wallpaper {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }

    [ComImport, Guid("B92B56A9-8B55-4E14-9A89-0199BBB6F93B"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IDesktopWallpaper {
        void SetWallpaper([MarshalAs(UnmanagedType.LPWStr)] string monitorID,
                          [MarshalAs(UnmanagedType.LPWStr)] string wallpaper);
        [return: MarshalAs(UnmanagedType.LPWStr)] string GetWallpaper([MarshalAs(UnmanagedType.LPWStr)] string monitorID);
        [return: MarshalAs(UnmanagedType.LPWStr)] string GetMonitorDevicePathAt(uint monitorIndex);
        uint GetMonitorDevicePathCount();
        void GetMonitorRECT([MarshalAs(UnmanagedType.LPWStr)] string monitorID, out RECT displayRect);
        void SetBackgroundColor(uint color);
        uint GetBackgroundColor();
        void SetPosition(int position);
        int GetPosition();
    }

    public static class Api {
        public static IDesktopWallpaper Create() {
            Type t = Type.GetTypeFromCLSID(new Guid("C2CF3110-460E-4fc1-B9D0-8A1C0C9CC4BD"));
            return (IDesktopWallpaper)Activator.CreateInstance(t);
        }

        public static uint Count() { return Create().GetMonitorDevicePathCount(); }

        public static string IdAt(uint i) { return Create().GetMonitorDevicePathAt(i); }

        public static int[] RectAt(string id) {
            RECT r;
            Create().GetMonitorRECT(id, out r);
            return new int[] { r.Left, r.Top, r.Right, r.Bottom };
        }

        public static void Assign(string id, string path) { Create().SetWallpaper(id, path); }

        public static void AssignAll(string path) { Create().SetWallpaper(null, path); }

        public static void Place(int position) { Create().SetPosition(position); }
    }
}
'@

if (-not ('HD2Wallpaper.Api' -as [type])) {
    Add-Type -TypeDefinition $source -Language CSharp
}

function Get-Monitors {
    $count = [HD2Wallpaper.Api]::Count()
    $list = New-Object System.Collections.ArrayList
    for ($i = 0; $i -lt $count; $i++) {
        $id = [HD2Wallpaper.Api]::IdAt([uint32]$i)
        if ([string]::IsNullOrEmpty($id)) { continue }
        try { $rect = [HD2Wallpaper.Api]::RectAt($id) } catch { continue }
        $null = $list.Add([pscustomobject]@{
            index  = $i
            id     = $id
            left   = $rect[0]
            top    = $rect[1]
            width  = $rect[2] - $rect[0]
            height = $rect[3] - $rect[1]
        })
    }
    return $list
}

switch ($Mode) {
    'List' {
        $monitors = Get-Monitors
        # Force an array so a single monitor still serialises as a JSON list.
        ConvertTo-Json -InputObject @($monitors) -Depth 4 -Compress
    }

    'Set' {
        if (-not (Test-Path $Payload)) { throw "Payload file not found: $Payload" }
        $assignments = Get-Content -Path $Payload -Raw -Encoding UTF8 | ConvertFrom-Json
        $monitors = Get-Monitors

        # Position must be set before assigning, so the new images are not
        # briefly laid out with the previous (Span) rule.
        [HD2Wallpaper.Api]::Place($Position)

        $applied = 0
        foreach ($item in @($assignments)) {
            $target = $monitors | Where-Object { $_.index -eq $item.index } | Select-Object -First 1
            if ($null -eq $target) { Write-Warning "No monitor at index $($item.index)"; continue }
            if (-not (Test-Path $item.path)) { Write-Warning "Missing image: $($item.path)"; continue }
            [HD2Wallpaper.Api]::Assign($target.id, $item.path)
            $applied++
        }
        Write-Output "applied=$applied"
    }

    'Restore' {
        if ([string]::IsNullOrEmpty($Path)) { throw 'Restore requires -Path' }
        if (-not (Test-Path $Path)) { throw "Wallpaper not found: $Path" }
        [HD2Wallpaper.Api]::Place($Position)
        [HD2Wallpaper.Api]::AssignAll($Path)
        Write-Output 'restored'
    }
}
