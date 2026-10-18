// A Paper Shaders dither field used as texture: a slow wave behind the timeline, and a faster
// ripple behind the objects while a rewind runs. It holds still for people who ask for less motion.

import { Dithering } from "@paper-design/shaders-react";
import { useEffect, useState } from "react";

const query = "(prefers-reduced-motion: reduce)";

export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => typeof matchMedia === "function" && matchMedia(query).matches);
  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const mq = matchMedia(query);
    const on = () => setReduced(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return reduced;
}

export type Props = { mode?: "calm" | "rewinding"; className?: string };

export default function DitherField({ mode = "calm", className }: Props) {
  const reduced = useReducedMotion();
  const rewinding = mode === "rewinding";
  return (
    <Dithering
      aria-hidden
      className={`dither ${className ?? ""}`}
      colorBack="#00000000"
      colorFront={rewinding ? "#5ED39A33" : "#F3EAD31F"}
      shape={rewinding ? "ripple" : "wave"}
      type="4x4"
      size={2}
      scale={rewinding ? 0.9 : 0.7}
      speed={reduced ? 0 : rewinding ? 0.9 : 0.12}
      minPixelRatio={1}
      maxPixelCount={1600 * 900}
    />
  );
}
