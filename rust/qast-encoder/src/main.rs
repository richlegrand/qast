use std::env;
use std::io::{self, Write};
use std::process::{Command, Stdio};
use std::thread;

#[derive(Debug)]
struct Opts {
    sources: Vec<String>,
    is_live: bool,
    duration: Option<f64>,
    aspect: f64,
    has_audio: bool,
}

fn usage() -> &'static str {
    "qast-encoder\n\nUsage:\n  qast-encoder --source <URL_OR_PATH> [--source ...] [--live] [--duration <secs>] [--aspect <factor>] [--no-audio]\n\nFlags:\n  --source <value>   Input source (repeatable)\n  --live             Enable real-time input pacing (-re)\n  --duration <secs>  Stop after N seconds\n  --aspect <factor>  Aspect correction (default 1.0)\n  --no-audio         Inject silent audio track\n  -h, --help         Show this help\n"
}

fn parse_args() -> Result<Opts, String> {
    let mut args = env::args().skip(1);
    let mut sources = Vec::new();
    let mut is_live = false;
    let mut duration = None;
    let mut aspect = 1.0_f64;
    let mut has_audio = true;

    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--source" => {
                let v = args
                    .next()
                    .ok_or_else(|| "--source requires a value".to_string())?;
                sources.push(v);
            }
            "--live" => is_live = true,
            "--duration" => {
                let raw = args
                    .next()
                    .ok_or_else(|| "--duration requires a value".to_string())?;
                let parsed = raw
                    .parse::<f64>()
                    .map_err(|_| format!("invalid --duration value: {raw}"))?;
                if !(parsed.is_finite() && parsed > 0.0) {
                    return Err(format!("invalid --duration value: {raw}"));
                }
                duration = Some(parsed);
            }
            "--aspect" => {
                let raw = args
                    .next()
                    .ok_or_else(|| "--aspect requires a value".to_string())?;
                let parsed = raw
                    .parse::<f64>()
                    .map_err(|_| format!("invalid --aspect value: {raw}"))?;
                if !(parsed.is_finite() && parsed > 0.0) {
                    return Err(format!("invalid --aspect value: {raw}"));
                }
                aspect = parsed;
            }
            "--no-audio" => has_audio = false,
            "--has-audio" => has_audio = true,
            "-h" | "--help" => {
                print!("{}", usage());
                std::process::exit(0);
            }
            other => return Err(format!("unknown argument: {other}")),
        }
    }

    if sources.is_empty() {
        return Err("at least one --source is required".to_string());
    }

    Ok(Opts {
        sources,
        is_live,
        duration,
        aspect,
        has_audio,
    })
}

fn ffmpeg_args(opts: &Opts) -> Vec<String> {
    let mut args = vec![
        "-y".to_string(),
        "-hide_banner".to_string(),
        "-nostdin".to_string(),
        "-loglevel".to_string(),
        "warning".to_string(),
        "-stats".to_string(),
    ];

    if opts.is_live {
        args.push("-re".to_string());
    }

    for src in &opts.sources {
        args.push("-i".to_string());
        args.push(src.clone());
    }

    if !opts.has_audio {
        args.push("-f".to_string());
        args.push("lavfi".to_string());
        args.push("-i".to_string());
        args.push("anullsrc=r=44100:cl=stereo".to_string());
    }

    if let Some(d) = opts.duration {
        args.push("-t".to_string());
        args.push(d.to_string());
    }

    let mut filters = Vec::new();
    if (opts.aspect - 1.0).abs() > f64::EPSILON {
        filters.push(format!("setsar={}", opts.aspect));
    }
    filters.push("scale=1920:1080:force_original_aspect_ratio=decrease".to_string());
    filters.push("pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black".to_string());

    args.push("-vf".to_string());
    args.push(filters.join(","));

    args.extend_from_slice(&[
        "-c:v".to_string(),
        "libx264".to_string(),
        "-preset".to_string(),
        "ultrafast".to_string(),
        "-b:v".to_string(),
        "5M".to_string(),
        "-r".to_string(),
        "30".to_string(),
        "-g".to_string(),
        "60".to_string(),
        "-c:a".to_string(),
        "aac".to_string(),
        "-ar".to_string(),
        "44100".to_string(),
        "-ac".to_string(),
        "2".to_string(),
        "-b:a".to_string(),
        "128k".to_string(),
        "-shortest".to_string(),
        "-muxdelay".to_string(),
        "0".to_string(),
        "-muxpreload".to_string(),
        "0".to_string(),
        "-flush_packets".to_string(),
        "1".to_string(),
        "-f".to_string(),
        "mpegts".to_string(),
        "pipe:1".to_string(),
    ]);

    args
}

fn main() {
    let opts = match parse_args() {
        Ok(v) => v,
        Err(e) => {
            eprintln!("qast-encoder: {e}");
            eprintln!();
            eprintln!("{}", usage());
            std::process::exit(2);
        }
    };

    let use_stdin = opts.sources.iter().any(|s| s == "pipe:0");
    let args = ffmpeg_args(&opts);

    let mut cmd = Command::new("ffmpeg");
    cmd.args(&args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(if use_stdin {
            Stdio::inherit()
        } else {
            Stdio::null()
        });

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("qast-encoder: failed to start ffmpeg: {e}");
            eprintln!("qast-encoder: install ffmpeg and ensure it is in PATH");
            std::process::exit(86);
        }
    };

    let mut child_stdout = child.stdout.take().expect("child stdout should be piped");
    let mut child_stderr = child.stderr.take().expect("child stderr should be piped");

    let stderr_thread = thread::spawn(move || {
        let mut stderr = io::stderr();
        let _ = io::copy(&mut child_stderr, &mut stderr);
        let _ = stderr.flush();
    });

    let mut stdout = io::stdout();
    let mut broken_pipe = false;
    if let Err(e) = io::copy(&mut child_stdout, &mut stdout) {
        if e.kind() == io::ErrorKind::BrokenPipe {
            broken_pipe = true;
        } else {
            eprintln!("qast-encoder: failed to stream encoder output: {e}");
            let _ = child.kill();
        }
    }
    let _ = stdout.flush();

    if broken_pipe {
        let _ = child.kill();
    }

    let status = match child.wait() {
        Ok(s) => s,
        Err(e) => {
            eprintln!("qast-encoder: failed waiting for ffmpeg: {e}");
            std::process::exit(87);
        }
    };

    let _ = stderr_thread.join();

    if broken_pipe {
        std::process::exit(0);
    }

    if let Some(code) = status.code() {
        std::process::exit(code);
    }

    std::process::exit(1);
}
