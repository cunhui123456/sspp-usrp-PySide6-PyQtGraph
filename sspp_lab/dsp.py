# 当前 SSPP 实验软件模块；源自已验证实现，老师原件保留在 historical/teacher_20260908/。
"""Complex lock-in and within-session relative phase; no hardware access."""
import math
import numpy as np


# 基于 RX 硬件 tick 检查逐帧无丢样/重叠；软件样点数单独累计。
class SampleContinuity:
    # 首帧允许任意设备 tick，之后严格要求前一帧末尾与后一帧开头相接。
    def __init__(self):
        self.next_tick = None
        self.samples = 0

    # tick 使用同一实际 RX 采样率换算；缺一个样本也拒绝连续相位链。
    def check(self, tick, count):
        if count <= 0:
            raise ValueError('Nonpositive sample count')
        if self.next_tick is not None and tick != self.next_tick:
            raise RuntimeError(f'RX timestamp discontinuity: expected {self.next_tick}, got {tick}')
        self.next_tick = int(tick) + int(count)
        self.samples += int(count)


class TimestampDiscontinuity(RuntimeError):
    """Keep the rejected frame's original timestamp separate from accepted IQ."""
    def __init__(self, evidence):
        self.evidence = evidence
        super().__init__(
            f"RX timestamp discontinuity at master clock: expected "
            f"{evidence['expected_device_tick_at_master_rate']}, got "
            f"{evidence['device_tick_at_master_rate']} "
            f"(delta {evidence['delta_master_ticks']} ticks, "
            f"{evidence['delta_samples']:g} samples)")


class HardwareTimeContinuity:
    """Validate RX continuity in native clock ticks, never rounded sample ticks.

    An arbitrary first timestamp need not lie on an integer RX-sample boundary.
    Rounding *each absolute* timestamp at RX rate can then alternate across a
    half-sample boundary. Native master-clock ticks preserve the timestamp's
    offset and require exact N-tick increments for every accepted sample.
    """
    def __init__(self, master_clock_rate_hz, sample_rate_hz):
        self.master_clock_rate_hz = float(master_clock_rate_hz)
        self.sample_rate_hz = float(sample_rate_hz)
        if not all(math.isfinite(v) and v > 0 for v in
                   (self.master_clock_rate_hz, self.sample_rate_hz)):
            raise ValueError('Timestamp clock and sample rates must be positive and finite')
        ratio = self.master_clock_rate_hz / self.sample_rate_hz
        nearest = round(ratio)
        # Allow only double-representation error, not a fractional tick/sample.
        if nearest < 1 or not math.isclose(
                self.master_clock_rate_hz, self.sample_rate_hz * nearest,
                rel_tol=8 * math.ulp(1.0), abs_tol=0):
            raise ValueError('Unsupported timestamp clock/RX rate ratio: '
                             'continuous capture requires integer master-clock ticks per sample')
        self.ticks_per_sample = nearest
        self.next_tick = None
        self.samples = 0

    def policy(self):
        return {'method': 'native_master_clock_ticks',
                'policy': 'exact_integer_tick_continuity_no_sample_tolerance',
                'master_clock_rate_hz': self.master_clock_rate_hz,
                'sample_rate_hz': self.sample_rate_hz,
                'ticks_per_sample': self.ticks_per_sample,
                'legacy_sample_ticks': 'device_tick_at_rx_rate retains independent absolute rounding; not used for continuity'}

    def check(self, time_spec, count):
        if isinstance(count, bool) or count <= 0 or count != int(count):
            raise ValueError('Nonpositive or noninteger sample count')
        # UHD keeps whole/fractional seconds separately. Do not collapse them
        # into get_real_secs()*clock, which loses precision at long uptimes.
        full = int(time_spec.get_full_secs())
        fraction = float(time_spec.get_frac_secs())
        if not math.isfinite(fraction):
            raise ValueError('Nonfinite RX timestamp')
        tick = int(time_spec.to_ticks(self.master_clock_rate_hz))
        delta = None if self.next_tick is None else tick - self.next_tick
        frame = {'start_sample': self.samples, 'count': int(count),
                 'device_tick_at_rx_rate': int(time_spec.to_ticks(self.sample_rate_hz)),
                 'device_tick_at_master_rate': tick,
                 'master_clock_rate_hz': self.master_clock_rate_hz,
                 'ticks_per_sample': self.ticks_per_sample,
                 'time_spec_full_secs': full, 'time_spec_frac_secs': fraction,
                 'expected_device_tick_at_master_rate': self.next_tick,
                 'delta_master_ticks': delta,
                 'delta_samples': None if delta is None else delta / self.ticks_per_sample}
        if delta is not None and delta != 0:
            raise TimestampDiscontinuity(frame)
        self.next_tick = tick + int(count) * self.ticks_per_sample
        self.samples += int(count)
        return frame


# 将目标单音乘以反向复指数并加窗平均，得到复系数 z 与目标相干功率占比估计。
# 输入 start_sample 是本次连续会话的全局样点编号，不是窗口内从零开始的编号。
def lockin(iq, start_sample, rate, tone):
    """Phase origin is the first RX sample of this uninterrupted session."""
    x = np.asarray(iq)
    if len(x) < 8 or not np.isfinite(x).all():
        raise ValueError('Invalid IQ window')
    n = np.arange(len(x), dtype=np.float64)
    # Keeping the global origin avoids a phase reset at every receive call.
    # 全局编号先取周期余数，避免每个 recv 窗口从相位零点开始而造成伪跳变。
    phase0 = np.remainder(start_sample * (tone / rate), 1.0)
    # x × exp(-j2πft) 将目标 tone 搬到直流；复均值的模/角分别给出幅度/相位。
    reference = np.exp(-2j*np.pi*(phase0+n*(tone/rate)))
    w = np.hanning(len(x))
    z = complex(np.sum(w*x*reference)/w.sum())
    power = float(np.mean(np.abs(x)**2))
    # 单音复系数功率/整窗平均功率是相干占比估计；有限窗噪声下不是严格 SNR。
    fraction = min(1., abs(z)**2/max(power, 1e-30))
    return z, fraction


# 计算静止参考幅值、解缠相位、幅度 SD 和相位漂移；拟合漂移只用于质量报告。
def baseline_metrics(rows):
    t = np.array([x['t_s'] for x in rows])
    z = np.array([complex(x['i'], x['q']) for x in rows])
    if len(z) < 25 or t[-1]-t[0] < 1:
        raise ValueError('Insufficient static baseline')
    amplitude = np.abs(z)
    if np.any(amplitude <= 1e-12):
        raise ValueError('Vanishing baseline amplitude')
    phase = np.array([x['phase_unwrapped_deg'] for x in rows])
    # 拟合 φ(t)=斜率×(t-均值)+截距；单位为 degree/s，中心化降低数值误差。
    coef = np.polyfit(t-t.mean(), phase, 1)
    # 仅用残差统计稳定性；不会从后续曲线自动减去此漂移。
    residual = phase-np.polyval(coef, t-t.mean())
    return {
        'start_s': float(t[0]), 'end_s': float(t[-1]), 'windows': len(rows),
        'reference_amplitude': float(np.median(amplitude)),
        'reference_phase_unwrapped_deg': float(np.mean(phase)),
        'amplitude_sd_dB': float(np.std(20*np.log10(amplitude), ddof=1)),
        'phase_linear_drift_deg_per_s': float(coef[0]),
        'phase_residual_sd_deg': float(np.std(residual, ddof=1)),
        'phase_total_span_deg': float(np.ptp(phase)),
        'min_coherent_fraction': float(min(x['coherent_fraction'] for x in rows)),
        'drift_correction_applied': False,
    }


# 逐窗维护相位连续性和初始参考；每帧只生成一条处理记录。
class RelativeMonitor:
    # 默认先等待 2 s，再积累 5 s 静止基线；等待段仍保存，但不作参考。
    def __init__(self, rate, tone, settle_s=2., baseline_s=5.):
        self.rate, self.tone = rate, tone
        self.settle_s, self.baseline_s = settle_s, baseline_s
        self.rows = []
        self.baseline = None
        self.last_angle = None
        self.unwrapped = 0.

    # 锁相 → 相邻相位最短差累加 → 窗口中心时间 → 建立基线 → 相对幅相。
    def process(self, iq, start_sample, label):
        z, fraction = lockin(iq, start_sample, self.rate, self.tone)
        angle = float(np.angle(z))
        if self.last_angle is None:
            self.unwrapped = angle
            jump = 0.
        else:
            # 把相邻差映射到 [-π,π] 再累加；真实相邻变化超过 180° 时无法唯一解缠。
            jump = float(np.angle(np.exp(1j*(angle-self.last_angle))))
            self.unwrapped += jump
        self.last_angle = angle
        # 时间戳用当前窗口中心，单位秒；以首个 RX 样本为 t=0。
        t = (start_sample+(len(iq)-1)/2)/self.rate
        row = {'t_s': float(t), 'start_sample': int(start_sample), 'count': len(iq),
               'label': label, 'i': z.real, 'q': z.imag,
               'amplitude_dBFS_est': float(20*np.log10(max(abs(z), 1e-15))),
               'phase_unwrapped_deg': float(np.degrees(self.unwrapped)),
               'phase_step_deg': float(np.degrees(jump)), 'coherent_fraction': fraction,
               'relative_amplitude_dB': None, 'relative_phase_deg': None,
               'wrapped_relative_phase_deg': None}
        self.rows.append(row)
        if self.baseline is None and t >= self.settle_s+self.baseline_s:
            selected = [x for x in self.rows if self.settle_s <= x['t_s'] < self.settle_s+self.baseline_s]
            self.baseline = baseline_metrics(selected)
        if self.baseline is not None:
            b = self.baseline
            # 相对幅度 = 20log10(|z|/初始参考幅度)，相对相位 = 解缠相位-初始参考相位。
            row['relative_amplitude_dB'] = float(20*np.log10(max(abs(z), 1e-15)/b['reference_amplitude']))
            row['relative_phase_deg'] = row['phase_unwrapped_deg']-b['reference_phase_unwrapped_deg']
            # 另存 [-180°,180°) 表示便于查看；连续分析使用未折返的 relative_phase_deg。
            row['wrapped_relative_phase_deg'] = float((row['relative_phase_deg']+180)%360-180)
        return row


# 工程预检门槛，不是科学显著性阈值；基线差则拒绝继续解释相位。
def baseline_problems(b):
    """Predeclared pilot gates, not scientific detection/significance thresholds."""
    failures = []
    if b['min_coherent_fraction'] < .5: failures.append('Baseline tone fraction below 0.5')
    if b['amplitude_sd_dB'] > .5: failures.append('Baseline amplitude SD exceeds 0.5 dB')
    if abs(b['phase_linear_drift_deg_per_s']) > 1.: failures.append('Baseline phase drift exceeds 1 degree/s')
    if b['phase_residual_sd_deg'] > 3.: failures.append('Baseline phase residual SD exceeds 3 degrees')
    return failures


# 按操作者标签汇总同会话相对读数；统计窗不是独立物理重复试验。
def summarize_labels(rows, baseline):
    if baseline is None: return {}
    groups = {}
    for row in rows:
        if row['t_s'] < baseline['end_s'] or row['label'].startswith('transition'):
            continue
        # transition 标签被排除；其余标签只描述操作者提供的状态，无法证明动作准确完成。
        groups.setdefault(row['label'], []).append(row)
    out = {}
    for label, part in groups.items():
        z = np.array([complex(x['i'], x['q']) for x in part])
        a = 20*np.log10(np.maximum(np.abs(z), 1e-15)/baseline['reference_amplitude'])
        ph = np.array([x['phase_unwrapped_deg'] for x in part])-baseline['reference_phase_unwrapped_deg']
        out[label] = {'windows': len(part), 'start_s': part[0]['t_s'], 'end_s': part[-1]['t_s'],
                      'amplitude_mean_dB': float(a.mean()), 'amplitude_sd_dB': float(a.std(ddof=1)) if len(a)>1 else None,
                      'phase_mean_deg': float(ph.mean()), 'phase_sd_deg': float(ph.std(ddof=1)) if len(ph)>1 else None,
                      'min_coherent_fraction': min(x['coherent_fraction'] for x in part)}
    return out
