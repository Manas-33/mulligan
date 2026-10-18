// The shader is decoration, so it loads in its own chunk after the page and renders nothing until then.

import { lazy, Suspense } from "react";
import type { Props } from "./DitherField.tsx";

const Field = lazy(() => import("./DitherField.tsx"));

export function DitherField(props: Props) {
  return (
    <Suspense fallback={null}>
      <Field {...props} />
    </Suspense>
  );
}
