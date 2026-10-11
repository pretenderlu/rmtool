/* Exact 3.29.0.152 layout, verified in both color-device xochitl binaries. */
#define EXPECTED_FIRMWARE "20261009073202"
#define MOVE_XOCHITL_SHA256 "09ba8581a50d96c19b360606ee0288b8863faec620036c8eda3b2a0b5cc2d2e2"
#define PAPER_PRO_XOCHITL_SHA256 "7aa5d67da6850d7021ade5a2232edeb5f34550236eca6f7a102f37f962dd4bd2"

static const uint8_t TARGET_PATTERN[] = {
    0x3f,0x23,0x03,0xd5,0xfd,0x7b,0xbb,0xa9,0xfd,0x03,0x00,0x91,
    0xf3,0x53,0x01,0xa9,0xf4,0x03,0x01,0xaa,0xf7,0x63,0x03,0xa9,
    0xf8,0x03,0x00,0xaa,0x20,0x00,0x40,0xf9,0xf9,0x23,0x00,0xf9,
    0x80,0x00,0x00,0xb4,
};

static int is_cjk(uintptr_t scene, int index) {
    uintptr_t text=0, glyphs=0;
    int64_t count=0;
    uint16_t ch=0;
    if(!scene || index<0)return 0;
    /* 3.29 passes a selection object pointing to the old glyph container. */
    memcpy(&text,(const void *)scene,sizeof(text));
    if(!text)return 0;
    memcpy(&glyphs,(const void *)(text+8),sizeof(glyphs));
    memcpy(&count,(const void *)(text+16),sizeof(count));
    if(!glyphs || index>=count)return 0;
    memcpy(&ch,(const void *)(glyphs+(uintptr_t)index*0x38+0x30),sizeof(ch));
    return (ch>=0x4e00&&ch<=0x9fff)||(ch>=0x3400&&ch<=0x4dbf)||
           (ch>=0xf900&&ch<=0xfaff)||(ch>=0x3000&&ch<=0x303f);
}

static int preserve_cjk_range(uintptr_t scene, const void *range) {
    uintptr_t sub=0;
    int64_t count=0;
    int32_t first=0;
    if(!range)return 0;
    memcpy(&sub,(const uint8_t *)range+8,sizeof(sub));
    memcpy(&count,(const uint8_t *)range+16,sizeof(count));
    if(!sub || count<=0 || count>=100000)return 0;
    /* Range records grew to 32 bytes; the first glyph index remains at +0. */
    memcpy(&first,(const void *)sub,sizeof(first));
    return is_cjk(scene,first);
}
