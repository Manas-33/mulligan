// The hand-drawn object for each system, the same art as the Paper design. Decorative: the system
// name is always written next to it, so the SVGs are aria-hidden.

import type { CSSProperties } from "react";
import type { System } from "../types.ts";

type Props = { system: System; size?: number; className?: string; style?: CSSProperties };

export function SystemObject({ system, size = 40, className, style }: Props) {
  const Art = ART[system] ?? ART.upstream;
  return <Art size={size} className={`object object-${system} ${className ?? ""}`} style={style} />;
}

type ArtProps = { size: number; className: string; style?: CSSProperties };

const svg = (vw: number, vh: number, size: number) => ({
  width: size,
  height: Math.round((size * vh) / vw),
  viewBox: `0 0 ${vw} ${vh}`,
  fill: "none",
  "aria-hidden": true,
  focusable: false,
});

const Shadow = ({ cx, cy, rx, ry }: { cx: number; cy: number; rx: number; ry: number }) => (
  <ellipse className="object-shadow" cx={cx} cy={cy} rx={rx} ry={ry} fill="#000" fillOpacity={0.55} />
);

const ART: Record<System, (p: ArtProps) => React.JSX.Element> = {
  postgres: ({ size, ...p }) => (
    <svg {...svg(100, 114, size)} {...p}>
      <Shadow cx={50} cy={104} rx={36} ry={7} />
      <path d="M16 24 V84 A34 12 0 0 0 84 84 V24 Z" fill="#7DB3FF" />
      <path d="M16 24 V84 A34 12 0 0 0 34 94.6 V35 A34 12 0 0 1 16 24 Z" fill="#9CC6FF" />
      <path d="M66 35.2 A34 12 0 0 0 84 24 V84 A34 12 0 0 1 66 94.8 Z" fill="#5C97EE" />
      <path d="M16 44 A34 12 0 0 0 84 44 M16 64 A34 12 0 0 0 84 64" stroke="#3F78D0" strokeWidth={3} strokeLinecap="round" />
      <ellipse cx={50} cy={24} rx={34} ry={12} fill="#C6DDFF" />
      <ellipse cx={44} cy={21} rx={12} ry={3.5} fill="#fff" fillOpacity={0.55} />
      <circle cx={72} cy={52} r={3} fill="#EDEAE0" />
      <circle cx={72} cy={72} r={3} fill="#EDEAE0" />
    </svg>
  ),
  github: ({ size, ...p }) => (
    <svg {...svg(110, 110, size)} {...p}>
      <Shadow cx={56} cy={98} rx={38} ry={7} />
      <g transform="rotate(-10 55 55)">
        <path d={TICKET} fill="#2F9E68" transform="translate(4 7)" />
        <path d={TICKET} fill="#5ED39A" />
        <path d="M30 26 h48" stroke="#8BE5B8" strokeWidth={3} strokeLinecap="round" />
        <circle cx={44} cy={48} r={11} stroke="#0D0D0C" strokeWidth={4} />
        <circle cx={44} cy={48} r={3.5} fill="#0D0D0C" />
        <path d="M62 44 h18 M62 53 h12" stroke="#0D0D0C" strokeWidth={4} strokeLinecap="round" />
      </g>
    </svg>
  ),
  notion: ({ size, ...p }) => (
    <svg {...svg(90, 110, size)} {...p}>
      <Shadow cx={45} cy={100} rx={32} ry={6} />
      <g transform="rotate(6 45 50)">
        <rect x={20} y={14} width={56} height={74} rx={6} fill="#B8B4A7" />
        <path d="M12 10 a6 6 0 0 1 6 -6 h36 l18 18 v62 a6 6 0 0 1 -6 6 h-48 a6 6 0 0 1 -6 -6 z" fill="#EDEAE0" />
        <path d="M54 4 v12 a6 6 0 0 0 6 6 h12 z" fill="#CFCBBE" />
        <path d="M22 36 h38 M22 46 h40 M22 56 h30 M22 66 h36" stroke="#A9A598" strokeWidth={3.5} strokeLinecap="round" />
        <circle cx={26} cy={22} r={5} fill="#0D0D0C" />
      </g>
    </svg>
  ),
  email: ({ size, ...p }) => (
    <svg {...svg(116, 100, size)} {...p}>
      <Shadow cx={58} cy={90} rx={42} ry={7} />
      <g transform="rotate(-6 58 48)">
        <rect x={14} y={26} width={88} height={56} rx={8} fill="#7E66E0" transform="translate(4 7)" />
        <rect x={14} y={26} width={88} height={56} rx={8} fill="#B9A6FF" />
        <path d="M18 30 L58 58 L98 30" stroke="#8A73EA" strokeWidth={5} strokeLinecap="round" strokeLinejoin="round" />
        <path d="M18 78 L46 52 M98 78 L70 52" stroke="#A08BF7" strokeWidth={3.5} strokeLinecap="round" />
        <circle cx={58} cy={58} r={8} fill="#0D0D0C" />
      </g>
    </svg>
  ),
  payments: ({ size, ...p }) => (
    <svg {...svg(116, 100, size)} {...p}>
      <Shadow cx={58} cy={90} rx={42} ry={7} />
      <g transform="rotate(8 58 48)">
        <rect x={12} y={22} width={92} height={58} rx={9} fill="#C23545" transform="translate(4 7)" />
        <rect x={12} y={22} width={92} height={58} rx={9} fill="#FF5C6C" />
        <rect x={12} y={34} width={92} height={10} fill="#D9404F" />
        <rect x={24} y={52} width={18} height={14} rx={3} fill="#EDEAE0" />
        <path d="M27 59 h12 M33 52 v14" stroke="#C9C5B8" strokeWidth={1.5} />
        <path d="M52 64 h36" stroke="#FFB3BB" strokeWidth={4} strokeLinecap="round" />
        <path d="M18 26 h40" stroke="#FF8A96" strokeWidth={2.5} strokeLinecap="round" />
      </g>
    </svg>
  ),
  upstream: ({ size, ...p }) => (
    <svg {...svg(110, 100, size)} {...p}>
      <Shadow cx={55} cy={92} rx={38} ry={6} />
      <g transform="rotate(-4 55 46)">
        <path d={BUBBLE} fill="#9A9481" transform="translate(4 6)" />
        <path d={BUBBLE} fill="#D8D2BF" />
        <circle cx={38} cy={44} r={6} fill="#0D0D0C" />
        <circle cx={56} cy={44} r={6} fill="#0D0D0C" />
        <circle cx={74} cy={44} r={6} fill="#0D0D0C" />
      </g>
    </svg>
  ),
  mulligan: ({ size, ...p }) => <Flag size={size} {...p} />,
};

const TICKET =
  "M18 30 a8 8 0 0 1 8 -8 h58 a8 8 0 0 1 8 8 v10 a8 8 0 0 0 0 16 v10 a8 8 0 0 1 -8 8 h-58 a8 8 0 0 1 -8 -8 v-10 a8 8 0 0 0 0 -16 z";
const BUBBLE = "M24 16 h62 a14 14 0 0 1 14 14 v28 a14 14 0 0 1 -14 14 h-40 l-16 14 v-14 h-6 a14 14 0 0 1 -14 -14 v-28 a14 14 0 0 1 14 -14 z";

/** The red flag that marks a held action (and the logo). */
export function Flag({ size = 18, className, style }: { size?: number; className?: string; style?: CSSProperties }) {
  return (
    <svg {...svg(26, 40, size)} className={`flag ${className ?? ""}`} style={style}>
      <path d="M5 38V3" stroke="#F3EAD3" strokeWidth={2.4} strokeLinecap="round" />
      <path d="M6 3h16l-4 6 4 6H6z" fill="#FF5C6C" />
    </svg>
  );
}

export function Logo() {
  return (
    <svg width={26} height={30} viewBox="0 0 30 34" fill="none" aria-hidden focusable={false}>
      <path d="M8 30V3" stroke="#F3EAD3" strokeWidth={2.6} strokeLinecap="round" />
      <path d="M9 3h17l-4 6 4 6H9z" fill="#FF5C6C" />
      <path d="M19 6l-4 3 4 3V6zM24 6l-4 3 4 3V6z" fill="#F3EAD3" />
    </svg>
  );
}

export function RewindGlyph({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={Math.round(size * 0.65)} viewBox="0 0 40 26" aria-hidden focusable={false}>
      <path d="M19 1L1 13l18 12V1zM39 1L21 13l18 12V1z" fill="currentColor" />
    </svg>
  );
}
