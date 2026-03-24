// ============================================================
//  SparkFun SerLCD 20x4 (Qwiic) + ESP32 Thing Plus (Micro-B)
//  3D-Printable Enclosure — Parametric OpenSCAD
//
//  Two-part shell:
//    PART = "front"   → front bezel + LCD recess
//    PART = "back"    → back cover with ESP32 mounts + ports
//    PART = "both"    → renders both side-by-side for preview
//
//  Hardware required:
//    4× M3×10mm countersunk screws (shell corners)
//    4× M3×6mm hex standoffs      (LCD backplane gap)
//    2× 4-40×5mm machine screws   (ESP32 to bosses)
//    4× M3×4mm heat-set inserts   (optional, back shell)
//    1× 100mm Qwiic cable
//
//  Print settings:
//    Material : PETG or ABS
//    Layer h  : 0.2 mm
//    Walls    : 3 perimeters
//    Infill   : 20% gyroid
//    Front    : face-down, no supports
//    Back     : interior-up, light supports on USB cutout
// ============================================================

// ---- RENDER SELECTION ----
PART = "both";   // "front" | "back" | "both"

// ---- SOURCE BOARD DIMENSIONS ----
// SparkFun SerLCD 20x4  (from SparkFun dimensional drawing)
lcd_pcb_w   = 97.67;   // PCB width  (long axis)
lcd_pcb_h   = 60.23;   // PCB height (short axis)
lcd_pcb_thk = 1.6;     // PCB substrate thickness
lcd_total_d = 15.32;   // total board depth incl. LCD glass + backpack

// SparkFun ESP32 Thing Plus (from SparkFun graphical datasheet)
esp_w  = 64.77;   // 2.55"
esp_h  = 22.86;   // 0.9"
esp_thk= 1.6;     // PCB thickness
esp_module_h = 8;    // WROOM module protrudes ~8 mm above PCB top
esp_usb_w    = 9.0;  // micro-B connector width
esp_usb_h    = 5.5;  // micro-B connector height
esp_usb_from_bottom = 2.0; // connector offset from PCB bottom edge (approx)

// Thing Plus 4-40 mounting hole positions (from datasheet: 0.1" from edges)
esp_hole_inset  = 2.54;   // 0.1 inch from each edge
esp_hole_dia    = 3.2;    // 4-40 clearance hole

// Qwiic connector on ESP32: 4-pin JST SH, ~5mm wide, right side of board
qwiic_w = 5.5;
qwiic_h = 3.5;
qwiic_from_top_esp = 8.0; // approx distance from top edge of ESP32

// ---- ENCLOSURE PARAMETERS ----
wall       = 4.0;    // shell wall thickness
corner_r   = 10;     // outer corner radius
lip_d      = 2.0;    // tongue-and-groove mating lip depth
lip_w      = 1.5;    // lip width
clearance  = 0.2;    // PCB pocket clearance each side

// Front shell depths
lcd_recess_d = 2.0;  // recess in front face for LCD bezel
front_d      = lcd_total_d + 2.0; // front shell total depth

// Standoff gap between LCD back and ESP32 front
standoff_gap = 6.0;  // clears ATmega328P backpack (~4mm) with margin

// Back shell depth: ESP32 total height + clearance
esp_total_h  = esp_thk + esp_module_h;
back_d       = esp_total_h + 4.0 + wall; // 4mm clearance + back wall

// Outer dimensions (front face = reference plane)
outer_w = lcd_pcb_w + wall*2 + clearance*2;  // ~105.67
outer_h = lcd_pcb_h + wall*2 + clearance*2;  // ~68.23
total_d = front_d + standoff_gap + back_d;

// LCD visible window (smaller than PCB — leaves a 4mm bezel all round)
window_w = lcd_pcb_w - 8;
window_h = lcd_pcb_h - 8;

// M3 screw boss inset (corner standoffs)
boss_inset = 7;    // from outer corner
boss_od    = 7;    // outer diameter of boss cylinder
boss_id    = 3.4;  // M3 clearance / heat-set inner bore

// ---- HELPERS ----
module rounded_box(w, h, d, r) {
    hull() {
        for (x=[r, w-r]) for (y=[r, h-r])
            translate([x, y, 0]) cylinder(r=r, h=d, $fn=40);
    }
}

module corner_bosses(h, bore_d) {
    locs = [
        [boss_inset, boss_inset],
        [outer_w-boss_inset, boss_inset],
        [boss_inset, outer_h-boss_inset],
        [outer_w-boss_inset, outer_h-boss_inset]
    ];
    for (p=locs) translate([p[0], p[1], 0]) {
        difference() {
            cylinder(d=boss_od, h=h, $fn=32);
            translate([0, 0, -0.1]) cylinder(d=bore_d, h=h+0.2, $fn=24);
        }
    }
}

module corner_holes(h) {
    locs = [
        [boss_inset, boss_inset],
        [outer_w-boss_inset, boss_inset],
        [boss_inset, outer_h-boss_inset],
        [outer_w-boss_inset, outer_h-boss_inset]
    ];
    for (p=locs) translate([p[0], p[1], -0.1])
        cylinder(d=boss_id, h=h+0.2, $fn=24);
}

// ============================================================
// FRONT SHELL
// ============================================================
module front_shell() {
    difference() {
        // --- solid body ---
        rounded_box(outer_w, outer_h, front_d, corner_r);

        // --- hollow interior (LCD pocket) ---
        translate([wall, wall, wall])
            cube([lcd_pcb_w + clearance*2,
                  lcd_pcb_h + clearance*2,
                  front_d - wall + 0.1]);

        // --- LCD window opening in front face ---
        translate([(outer_w - window_w)/2,
                   (outer_h - window_h)/2,
                   -0.1])
            cube([window_w, window_h, lcd_recess_d + 0.2]);

        // Full through-hole for the display glass area
        // (inner window, slightly smaller than PCB window)
        inner_win_w = window_w - 4;
        inner_win_h = window_h - 4;
        translate([(outer_w - inner_win_w)/2,
                   (outer_h - inner_win_h)/2,
                   -0.1])
            cube([inner_win_w, inner_win_h, front_d]);

        // --- M3 corner through-holes ---
        corner_holes(front_d);

        // --- mating lip channel (female, cut from front shell) ---
        translate([wall-lip_w, wall-lip_w, front_d-lip_d])
            cube([outer_w - 2*(wall-lip_w),
                  outer_h - 2*(wall-lip_w),
                  lip_d + 0.1]);
    }

    // --- corner bosses (interior, M3 clearance) ---
    translate([0, 0, wall])
        corner_bosses(front_d - wall - lip_d, boss_id);
}

// ============================================================
// BACK SHELL
// ============================================================
module back_shell() {
    // ESP32 board centered in back shell (X center, Y centered)
    esp_x = (outer_w - esp_w) / 2;
    esp_y = wall + (lcd_pcb_h - esp_h) / 2 + clearance;  // vertically centered

    // USB cutout X: aligns to micro-B on ESP32
    // micro-B is at one short end of the Thing Plus, ~7mm from the end
    usb_x = esp_x + 4;   // ~4mm from short end of ESP32 PCB
    usb_z = wall + esp_usb_from_bottom;

    // Qwiic slot: right side wall, aligned to Qwiic JST on ESP32
    qwiic_y = esp_y + qwiic_from_top_esp;
    qwiic_z = wall + esp_thk;   // flush with PCB surface

    difference() {
        // --- solid body ---
        rounded_box(outer_w, outer_h, back_d, corner_r);

        // --- hollow interior ---
        translate([wall, wall, wall])
            cube([outer_w - wall*2,
                  outer_h - wall*2,
                  back_d - wall + 0.1]);

        // --- mating lip (male tongue on back shell) ---
        // carved slightly to create tongue; front female mates over it
        translate([wall-lip_w+lip_w, wall-lip_w+lip_w, back_d-lip_d-0.5])
            cube([outer_w - 2*(wall-lip_w+lip_w) + 0.01,
                  outer_h - 2*(wall-lip_w+lip_w) + 0.01,
                  lip_d + 0.1 + 0.5]);

        // --- M3 corner holes ---
        corner_holes(back_d);

        // --- micro-B USB cutout (back wall) ---
        translate([usb_x, -0.1, usb_z])
            cube([esp_usb_w + 1, wall + 0.2, esp_usb_h + 1]);

        // --- JST LiPo cutout (top wall) ---
        // LiPo JST is at the opposite end from USB on the Thing Plus
        lipo_x = esp_x + esp_w - 12;
        translate([lipo_x, outer_h - wall - 0.1, wall + esp_thk + 1])
            cube([10, wall + 0.2, 6]);

        // --- Qwiic side slot (right wall) ---
        translate([outer_w - wall - 0.1, qwiic_y, qwiic_z])
            cube([wall + 0.2, qwiic_w + 1, qwiic_h + 1]);

        // --- Reset button access hole ---
        reset_x = esp_x + esp_w - 4;
        translate([reset_x, -0.1, esp_y + esp_h/2 + wall])
            rotate([-90, 0, 0])
            cylinder(d=4.5, h=wall+0.2, $fn=24);

        // --- Boot/Button-0 access hole ---
        btn0_x = esp_x + 4;
        translate([btn0_x, -0.1, esp_y + esp_h/2 + wall])
            rotate([-90, 0, 0])
            cylinder(d=4.5, h=wall+0.2, $fn=24);

        // --- CHG LED window ---
        led1_x = esp_x + esp_w * 0.4;
        translate([led1_x, -0.1, esp_y + esp_h - 3 + wall])
            rotate([-90, 0, 0])
            cylinder(d=2.4, h=wall+0.2, $fn=16);

        // --- Status LED window ---
        led2_x = esp_x + esp_w * 0.6;
        translate([led2_x, -0.1, esp_y + esp_h - 3 + wall])
            rotate([-90, 0, 0])
            cylinder(d=2.4, h=wall+0.2, $fn=16);

        // --- Optional vent slots (bottom wall) ---
        for (vx = [outer_w*0.3, outer_w*0.45, outer_w*0.6]) {
            translate([vx - 5, -0.1, wall*0.4])
                cube([10, wall + 0.2, 5]);
        }
    }

    // --- ESP32 mounting bosses (4-40 threaded or clearance) ---
    // Two bosses matching the Thing Plus 4-40 hole pattern
    hole_positions = [
        [esp_x + esp_hole_inset,        esp_y + esp_hole_inset],
        [esp_x + esp_w - esp_hole_inset, esp_y + esp_hole_inset]
    ];
    for (p = hole_positions) {
        translate([p[0], p[1], wall]) {
            difference() {
                cylinder(d=6, h=esp_thk + 1.5, $fn=24);   // boss
                translate([0, 0, -0.1])
                    cylinder(d=2.8, h=esp_thk + 1.7, $fn=20);  // 4-40 bore
            }
        }
    }

    // --- Mating tongue (positive lip on back shell) ---
    // Thin wall ridge that fits inside front shell channel
    difference() {
        translate([wall-lip_w, wall-lip_w, back_d-lip_d])
            difference() {
                cube([outer_w - 2*(wall-lip_w),
                      outer_h - 2*(wall-lip_w),
                      lip_d]);
                translate([lip_w, lip_w, -0.1])
                    cube([outer_w - 2*(wall-lip_w) - 2*lip_w,
                          outer_h - 2*(wall-lip_w) - 2*lip_w,
                          lip_d + 0.2]);
            }
        // don't let tongue collide with corner bosses
        corner_holes(back_d + 1);
    }

    // --- corner M3 boss cylinders (with heat-set insert bore) ---
    corner_bosses(back_d - wall, 4.2);   // 4.2mm bore for M3 heat-set insert
}

// ============================================================
// RENDER
// ============================================================
if (PART == "front") {
    color("SteelBlue", 0.85) front_shell();
}
else if (PART == "back") {
    color("DimGray", 0.85) back_shell();
}
else {
    // Both side-by-side for preview
    color("SteelBlue", 0.85)
        translate([0, 0, 0]) front_shell();
    color("DimGray", 0.85)
        translate([outer_w + 10, 0, 0]) back_shell();
}

// ============================================================
// NOTES FOR SLICER
// ============================================================
// 1. Export each part separately:
//    Set PART = "front"  → export front_shell.stl
//    Set PART = "back"   → export back_shell.stl
//
// 2. Front shell: place window-face DOWN on print bed.
//    No supports needed. First layer on the window recess
//    gives a smooth display-facing surface.
//
// 3. Back shell: place open side (interior) UP.
//    Enable supports for the USB slot overhang only.
//    "Paint-on supports" or "support enforcers" recommended.
//
// 4. After printing:
//    a) Press M3×4mm heat-set inserts into back shell boss bores
//    b) Thread M3×6mm standoffs onto LCD PCB mounting points
//    c) Connect 100mm Qwiic cable between LCD and ESP32
//    d) Set ESP32 into back shell bosses, thread 4-40 screws
//    e) Mate front + back shells (tongue into groove)
//    f) Drive M3×10mm screws through front corners into inserts
// ============================================================
