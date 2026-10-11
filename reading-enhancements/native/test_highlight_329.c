#include <assert.h>
#include <stdint.h>
#include <string.h>
#include "highlight_snap_329.h"

int main(void) {
    uint8_t glyphs[3*0x38]={0};
    uint64_t text[3]={0,(uintptr_t)glyphs,3};
    uintptr_t scene=(uintptr_t)text;
    uint8_t records[2*32]={0};
    uint64_t range[3]={0,(uintptr_t)records,2};
    const uint16_t characters[]={0x4e2d,0x6587,'A'};
    for(int i=0;i<3;i++)memcpy(glyphs+i*0x38+0x30,&characters[i],2);
    assert(sizeof(uintptr_t)==8 && sizeof(TARGET_PATTERN)==40);
    assert(preserve_cjk_range((uintptr_t)&scene,range));
    for(int32_t index=-1;index<=3;index++) {
        memcpy(records,&index,sizeof(index));
        assert(preserve_cjk_range((uintptr_t)&scene,range)==(index==0||index==1));
    }
    int32_t index=0;
    memcpy(records,&index,sizeof(index));
    const uint16_t boundaries[]={0x33ff,0x3400,0x4dbf,0x4dc0,0x4dff,0x4e00,
                                0x9fff,0xa000,0xf8ff,0xf900,0xfaff,0xfb00,
                                0x2fff,0x3000,0x303f,0x3040,' ',0xd800};
    const int expected[]={0,1,1,0,0,1,1,0,0,1,1,0,0,1,1,0,0,0};
    for(unsigned i=0;i<sizeof(boundaries)/sizeof(*boundaries);i++) {
        memcpy(glyphs+0x30,&boundaries[i],2);
        assert(preserve_cjk_range((uintptr_t)&scene,range)==expected[i]);
    }
    memcpy(glyphs+0x30,&characters[0],2);
    assert(preserve_cjk_range((uintptr_t)&scene,range));
    assert(!preserve_cjk_range(0,range));
    assert(!preserve_cjk_range((uintptr_t)&scene,0));
    scene=0;
    assert(!preserve_cjk_range((uintptr_t)&scene,range));
    scene=(uintptr_t)text;
    text[1]=0;
    assert(!preserve_cjk_range((uintptr_t)&scene,range));
    text[1]=(uintptr_t)glyphs;
    text[2]=0;
    assert(!preserve_cjk_range((uintptr_t)&scene,range));
    text[2]=UINT64_MAX;
    assert(!preserve_cjk_range((uintptr_t)&scene,range));
    text[2]=3;
    range[1]=0;
    assert(!preserve_cjk_range((uintptr_t)&scene,range));
    range[1]=(uintptr_t)records;
    for(unsigned i=0;i<3;i++) {
        range[2]=(uint64_t[]){0,UINT64_MAX,100000}[i];
        assert(!preserve_cjk_range((uintptr_t)&scene,range));
    }
    return 0;
}
