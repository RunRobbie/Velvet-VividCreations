# Change log

## 2026-10-26 · v1.2 · Footer, navigation and responsive QA

- Rebuilt the public footer using one semantic, responsive structure rather than separate grid placements across breakpoints.
- Mobile: brand and centered CREATIONS, two equal link groups, centered Visit section, balanced separators and copyright.
- Tablet and phone desktop mode: brand above three aligned navigation sections.
- Desktop: cohesive wordmark and three navigation sections on a single row.
- Fixed Services page overflow at narrow widths by wrapping service headings and using a single-column list where required.
- Checked homepage, Services, Gallery, Booking, Contact, About and dashboard at 320, 390, 600, 768, 820, 980, 1120, 1121, 1365 and 1920 px: no horizontal overflow or JavaScript errors.
- Re-tested navigation menu and footer links. Five backend smoke tests passed.
- Updated both the actual application source and standalone offline preview.

## 2026-10-26 · Responsive design polish

- Updated both public and admin-facing source, not only the offline preview.
- Changed the navigation brand to compact V & V to avoid duplicating the complete hero logo.
- Centered `CREATIONS` below the footer wordmark using a shared logo lockup.
- Removed the timezone from the public footer and dashboard footer. The booking workflow still uses Melbourne's Eastern timezone internally and mentions it where appointment times need clarity.
- Replaced the sliced decorative image with a clean, lettering-free velvet asset; removed overlapping CSS-generated monogram.
- Introduced a responsive, mobile-specific logo treatment; desktop continues to use the approved maroon velvet-left hero artwork.
- Updated the standalone HTML preview to match production UI and keep demo-only interactions isolated.
- Validated layouts at mobile/tablet/desktop/wide breakpoints; five backend smoke tests passed.
