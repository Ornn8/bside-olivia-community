param(
    [Parameter(Mandatory = $true)][string]$MediaPath,
    [string]$ReportPath,
    [int]$ClientProcessId = 0,
    [int]$LauncherProcessId = 0,
    [string]$ClientProcessIdFile,
    # The splash never outlives these, painted client window or not.
    [double]$HoldAfterEndSeconds = 30,
    [double]$MaxSeconds = 60
)

$ErrorActionPreference = 'Stop'
$script:clock = [Diagnostics.Stopwatch]::StartNew()
$script:report = [ordered]@{ status = 'starting'; outcome = $null; loaded = $false; ended = $false; error = $null; geometry = $null }
$script:report.started_at = [DateTimeOffset]::UtcNow.ToString('o')
$script:exitCode = 0
$script:window = $null
$script:media = $null
$script:endedAt = -1.0

function Get-Elapsed { [Math]::Round($script:clock.Elapsed.TotalSeconds, 3) }

function Save-Report {
    if ($ReportPath) {
        try {
            $script:report.updated_at = [DateTimeOffset]::UtcNow.ToString('o')
            $script:report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ReportPath -Encoding UTF8
        } catch { } # Optional diagnostics must not prevent playback or closing.
    }
}

function Set-Outcome([string]$Outcome) {
    # The first outcome wins; the launcher reads it into launcher.jsonl as startup_splash.
    if (-not $script:report.outcome) {
        $script:report.outcome = $Outcome
        $script:report.elapsed_seconds = Get-Elapsed
    }
}

function Close-Splash([string]$Outcome) {
    $script:report.status = $Outcome
    Set-Outcome $Outcome
    Save-Report
    if ($script:window) { $script:window.Close() }
}

function Fail-Media([string]$Message) {
    $script:exitCode = 1
    $script:report.error = $Message
    Close-Splash 'error'
}

try {
    if ([Threading.Thread]::CurrentThread.ApartmentState -ne 'STA') {
        throw 'Run this script with powershell.exe -STA.'
    }
    $file = Get-Item -LiteralPath $MediaPath -ErrorAction Stop
    if ($file.PSIsContainer -or $file.Length -eq 0) { throw 'MediaPath must be a non-empty local media file.' }
    Add-Type -AssemblyName PresentationFramework
    Add-Type -ReferencedAssemblies System.Drawing @'
using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Drawing;
using System.Collections.Generic;
public static class OliviaStartupReady {
  public delegate bool Callback(IntPtr h, IntPtr p);
  [StructLayout(LayoutKind.Sequential)] public struct Rect { public int L,T,R,B; }
  [DllImport("user32.dll")] static extern bool EnumWindows(Callback cb, IntPtr p);
  [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr h, Callback cb, IntPtr p);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] static extern bool IsHungAppWindow(IntPtr h);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out Rect r);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr h, StringBuilder name, int size);
  [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  static bool Painted(IntPtr h, Rect r) {
    try {
      using(var bitmap=new Bitmap(r.R-r.L,r.B-r.T)) using(var graphics=Graphics.FromImage(bitmap)) {
        IntPtr dc=graphics.GetHdc(); bool painted;
        try { painted=PrintWindow(h,dc,2); } finally { graphics.ReleaseHdc(dc); }
        if(!painted) return false;
        var colors=new HashSet<int>();
        for(int y=80;y<bitmap.Height-20;y+=17) for(int x=30;x<bitmap.Width-20;x+=17) colors.Add(bitmap.GetPixel(x,y).ToArgb());
        return colors.Count>=16; // Blank loading surfaces cannot hand off the overlay.
      }
    } catch { return false; }
  }
  // PrintWindow waits on the client's window; probes run on a pool thread so the splash never stalls.
  static int busy; static long lastFound; static int lastPid; static long probes;
  public static void Begin(int pid) {
    if (System.Threading.Interlocked.CompareExchange(ref busy, 1, 0) != 0) return;
    System.Threading.ThreadPool.QueueUserWorkItem(state => {
      long handle = 0;
      try { handle = Find(pid); } catch { handle = 0; }
      System.Threading.Interlocked.Exchange(ref lastFound, handle);
      System.Threading.Interlocked.Exchange(ref lastPid, pid);
      System.Threading.Interlocked.Increment(ref probes);
      System.Threading.Interlocked.Exchange(ref busy, 0);
    });
  }
  public static long Probes { get { return System.Threading.Interlocked.Read(ref probes); } }
  public static long Result(int pid) {
    return System.Threading.Interlocked.CompareExchange(ref lastPid, 0, 0) == pid ? System.Threading.Interlocked.Read(ref lastFound) : 0;
  }
  public static long Find(int pid) {
    long found=0;
    EnumWindows((h,p)=> {
      uint owner; Rect r; GetWindowThreadProcessId(h,out owner); GetWindowRect(h,out r);
      if(owner!=pid || !IsWindowVisible(h) || IsIconic(h) || IsHungAppWindow(h) || r.R-r.L<600 || r.B-r.T<400) return true;
      EnumChildWindows(h,(c,q)=> {
        Rect cr; GetWindowRect(c,out cr); var name=new StringBuilder(256); GetClassName(c,name,256);
        string cls=name.ToString();
        if(IsWindowVisible(c) && cr.R-cr.L>=(r.R-r.L)*0.8 && cr.B-cr.T>=(r.B-r.T)*0.8 &&
           (cls.StartsWith("Chrome_RenderWidgetHostHWND") || (cls.StartsWith("Qt") && cls.EndsWith("QWindowIcon"))) && Painted(c,cr)) found=h.ToInt64();
        return found==0;
      },IntPtr.Zero);
      return found==0;
    },IntPtr.Zero);
    return found;
  }
}
'@

    # WPF WorkArea and Window coordinates are device-independent pixels (DIP).
    # Use the primary work area, excluding the taskbar, without mixing physical pixels.
    $work = [Windows.SystemParameters]::WorkArea
    $width = [Math]::Sqrt($work.Width * $work.Height / 6 * 16 / 9)
    $height = $width * 9 / 16
    $script:window = New-Object Windows.Window
    $window.Title = 'Community - Awakening Edition'
    $window.WindowStyle = 'None'
    $window.ResizeMode = 'NoResize'
    $window.WindowStartupLocation = 'Manual'
    $window.Width = $width
    $window.Height = $height
    $window.Left = $work.Left + ($work.Width - $width) / 2
    $window.Top = $work.Top + ($work.Height - $height) / 2
    $window.Background = [Windows.Media.Brushes]::Black
    $window.ShowInTaskbar = $true
    $window.Topmost = $false  # A slow start must not cover other windows.

    $script:media = New-Object Windows.Controls.MediaElement
    $media.LoadedBehavior = 'Manual'
    $media.UnloadedBehavior = 'Close'
    $media.Stretch = 'Uniform'
    $media.Volume = 1.0
    $media.IsMuted = $false
    $window.Content = $media

    $window.Add_Loaded({
        try {
            $scale = [Windows.PresentationSource]::FromVisual($window).CompositionTarget.TransformToDevice
            $script:report.geometry = [ordered]@{
                left = $window.Left; top = $window.Top
                width = $window.ActualWidth; height = $window.ActualHeight
                requested_width = $width; requested_height = $height
                work_left = $work.Left; work_top = $work.Top
                work_width = $work.Width; work_height = $work.Height
                dpi_scale_x = $scale.M11; dpi_scale_y = $scale.M22
            }
            $script:report.status = 'window_loaded'
            Save-Report
            $media.Source = [Uri]$file.FullName
            $media.Play()
        } catch { Fail-Media $_.Exception.Message }
    })
    $media.Add_MediaOpened({
        if (-not $media.HasVideo -or -not $media.NaturalDuration.HasTimeSpan -or $media.NaturalDuration.TimeSpan.TotalSeconds -le 0) {
            Fail-Media 'Media must contain a playable video with a finite duration.'
            return
        }
        $script:report.loaded = $true
        $script:report.status = 'loaded'
        $script:report.duration_seconds = $media.NaturalDuration.TimeSpan.TotalSeconds
        $script:report.has_audio = $media.HasAudio
        $script:report.video_width = $media.NaturalVideoWidth
        $script:report.video_height = $media.NaturalVideoHeight
        Save-Report
    })
    $media.Add_MediaEnded({
        $media.Pause() # Hold the final decoded frame; never loop the audio.
        $script:report.ended = $true
        $script:report.status = 'holding_last_frame'
        $script:endedAt = $script:clock.Elapsed.TotalSeconds
        $script:report.ended_after_seconds = Get-Elapsed
        Save-Report
        if ($ClientProcessId -le 0 -and -not $ClientProcessIdFile) { Set-Outcome 'ended'; $window.Close() }
    })
    $media.Add_MediaFailed({ param($sender, $eventArgs) Fail-Media $eventArgs.ErrorException.Message })
    $window.Add_PreviewKeyDown({
        param($sender, $eventArgs)
        if ($eventArgs.Key -eq [Windows.Input.Key]::Escape) {
            $eventArgs.Handled = $true
            Close-Splash 'skipped'
        }
    })
    $window.Add_PreviewMouseLeftButtonUp({
        param($sender, $eventArgs)
        $eventArgs.Handled = $true
        Close-Splash 'skipped' # A click dismisses the splash, like Esc.
    })
    $window.Add_Closed({
        $media.Close()
        if ($script:timer) { $script:timer.Stop() }
        if ($script:report.status -notin @('holding_last_frame', 'ready', 'timeout', 'client_exited', 'launcher_exited', 'skipped', 'error')) {
            $script:report.status = 'closed'
        }
        Set-Outcome 'closed'
        Save-Report
    })
    $script:readyCount = 0
    $script:seenProbes = [long]0
    $script:clientId = $ClientProcessId
    $script:timer = New-Object Windows.Threading.DispatcherTimer
    $timer.Interval = [TimeSpan]::FromMilliseconds(500)
    $timer.Add_Tick({
        if ($LauncherProcessId -gt 0 -and -not (Get-Process -Id $LauncherProcessId -ErrorAction SilentlyContinue)) {
            Close-Splash 'launcher_exited'; return
        }
        # Close even when the client window never paints (a black window must not keep the splash up).
        $elapsed = $script:clock.Elapsed.TotalSeconds
        if (($script:endedAt -ge 0 -and $elapsed - $script:endedAt -ge $HoldAfterEndSeconds) -or $elapsed -ge $MaxSeconds) {
            Close-Splash 'timeout'; return
        }
        if ($ClientProcessIdFile) {
            try {
                $candidate = [int](Get-Content -LiteralPath $ClientProcessIdFile -Raw -ErrorAction Stop)
                if ($candidate -gt 0 -and $candidate -ne $script:clientId) {
                    $script:clientId = $candidate
                    $script:readyCount = 0
                }
            } catch { } # The client PID is published after backend preparation.
        }
        if ($script:clientId -le 0) { return }
        if (-not (Get-Process -Id $script:clientId -ErrorAction SilentlyContinue)) {
            if ($ClientProcessIdFile -and $LauncherProcessId -gt 0) {
                $script:readyCount = 0; return # Keep the frame during a native-client retry.
            }
            Close-Splash 'client_exited'; return
        }
        if (-not $script:report.ended) { return }
        # Read the last finished probe, then ask for the next one; neither waits on the client.
        $probes = [OliviaStartupReady]::Probes
        if ($probes -ne $script:seenProbes) {
            $script:seenProbes = $probes
            $handle = [OliviaStartupReady]::Result($script:clientId)
            if ($handle -gt 0) { $script:readyCount++; $script:readyHandle = $handle } else { $script:readyCount = 0 }
        }
        [OliviaStartupReady]::Begin($script:clientId)
        if ($script:report.ended -and $script:readyCount -ge 3) {
            $script:report.main_window = $script:readyHandle
            $script:report.ready_after_seconds = Get-Elapsed
            $script:report.probe_count = $probes
            Close-Splash 'ready'
            $null = [OliviaStartupReady]::SetForegroundWindow([IntPtr]$script:readyHandle)
            return
        }
    })
    $timer.Start()
    $null = $window.ShowDialog()
} catch {
    Fail-Media $_.Exception.Message
} finally {
    if ($script:media) { $script:media.Close() }
}
exit $script:exitCode
