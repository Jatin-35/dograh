import { ReactNode } from 'react';

interface UnmaskedOnlyProps {
    // Whether the customer number was masked for this viewer: `null` until the
    // call report that says so has loaded (or if it could not be loaded).
    phoneMasked: boolean | null;
    children: ReactNode;
}

/**
 * Shows its children only once it is known that the number was NOT masked for this
 * viewer. The raw context blocks carry the number, so they must never appear
 * before the report has said whether masking applies (they would flash up for a
 * viewer who must not see them), and must stay hidden if the report cannot be
 * loaded. Anything other than an explicit `false` means hidden.
 */
export function UnmaskedOnly({ phoneMasked, children }: UnmaskedOnlyProps) {
    return phoneMasked === false ? <>{children}</> : null;
}
