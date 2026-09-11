/* Chinese highlighter range handling for the verified Move 3.28.0.172 build.
 *
 * The target signature and CJK-only range decision are adapted from
 * bbq191/rm-tweak commit 1ccbdb0 (Apache-2.0). The trampoline, identity
 * checks, SHA-256 implementation and configuration handling are rmtool code.
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#define EXPECTED_FIRMWARE "20260827113527"
#define EXPECTED_XOCHITL_SHA256 "5ba79d1b5656df1a771217d29a8d3938c40256be53361b10a0d17cd4752807f4"
#define CONFIG_PATH "/home/root/.config/remarkable/xochitl.conf"
#define PATCH_LEN 20

static const uint8_t TARGET_PATTERN[] = {
    0x3f,0x23,0x03,0xd5,0xfd,0x7b,0xbd,0xa9,0xfd,0x03,0x00,0x91,
    0xf5,0x13,0x00,0xf9,0xf5,0x03,0x00,0xaa,0x20,0x00,0x40,0xf9,
    0xf3,0x53,0x01,0xa9,0xf4,0x03,0x01,0xaa,0x80,0x00,0x00,0xb4,
    0x01,0x00,0x40,0xb9,
};

typedef struct { uint32_t h[8]; uint64_t bits; uint8_t block[64]; size_t used; } Sha256;
static const uint32_t K[64] = {
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
  0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
  0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
  0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
  0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
};
static uint32_t rotr(uint32_t x, unsigned n) { return (x >> n) | (x << (32 - n)); }
static void sha_block(Sha256 *s, const uint8_t *p) {
    uint32_t w[64], a,b,c,d,e,f,g,h;
    for (int i=0;i<16;i++) w[i]=((uint32_t)p[i*4]<<24)|((uint32_t)p[i*4+1]<<16)|((uint32_t)p[i*4+2]<<8)|p[i*4+3];
    for (int i=16;i<64;i++) { uint32_t x=w[i-15], y=w[i-2]; w[i]=w[i-16]+(rotr(x,7)^rotr(x,18)^(x>>3))+w[i-7]+(rotr(y,17)^rotr(y,19)^(y>>10)); }
    a=s->h[0];b=s->h[1];c=s->h[2];d=s->h[3];e=s->h[4];f=s->h[5];g=s->h[6];h=s->h[7];
    for (int i=0;i<64;i++) { uint32_t s1=rotr(e,6)^rotr(e,11)^rotr(e,25), ch=(e&f)^(~e&g), t1=h+s1+ch+K[i]+w[i], s0=rotr(a,2)^rotr(a,13)^rotr(a,22), maj=(a&b)^(a&c)^(b&c), t2=s0+maj; h=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2; }
    s->h[0]+=a;s->h[1]+=b;s->h[2]+=c;s->h[3]+=d;s->h[4]+=e;s->h[5]+=f;s->h[6]+=g;s->h[7]+=h;
}
static void sha_init(Sha256 *s) { static const uint32_t h[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19}; memcpy(s->h,h,sizeof(h));s->bits=0;s->used=0; }
static void sha_update(Sha256 *s,const uint8_t *p,size_t n) { s->bits+=(uint64_t)n*8; while(n){size_t take=64-s->used;if(take>n)take=n;memcpy(s->block+s->used,p,take);s->used+=take;p+=take;n-=take;if(s->used==64){sha_block(s,s->block);s->used=0;}} }
static void sha_final(Sha256 *s,char out[65]) { uint64_t bits=s->bits;s->block[s->used++]=0x80;if(s->used>56){while(s->used<64)s->block[s->used++]=0;sha_block(s,s->block);s->used=0;}while(s->used<56)s->block[s->used++]=0;for(int i=7;i>=0;i--)s->block[s->used++]=(uint8_t)(bits>>(i*8));sha_block(s,s->block);for(int i=0;i<8;i++)sprintf(out+i*8,"%08x",s->h[i]);out[64]=0; }

static int file_text_equals(const char *path, const char *expected) {
    FILE *f=fopen(path,"rb"); char buf[128]; size_t n; if(!f)return 0;n=fread(buf,1,sizeof(buf)-1,f);fclose(f);buf[n]=0;
    while(n && isspace((unsigned char)buf[n-1]))buf[--n]=0; return strcmp(buf,expected)==0;
}
static int xochitl_hash_matches(void) {
    FILE *f=fopen("/usr/bin/xochitl","rb"); uint8_t buf[65536]; size_t n; Sha256 s; char hex[65]; if(!f)return 0;
    sha_init(&s); while((n=fread(buf,1,sizeof(buf),f))>0)sha_update(&s,buf,n); if(ferror(f)){fclose(f);return 0;}fclose(f);sha_final(&s,hex);return strcmp(hex,EXPECTED_XOCHITL_SHA256)==0;
}
static int machine_matches(void) {
    FILE *f=fopen("/sys/devices/soc0/machine","rb"); char buf[256]; size_t n; if(!f)return 0;n=fread(buf,1,sizeof(buf)-1,f);fclose(f);buf[n]=0;
    while(n && isspace((unsigned char)buf[n-1]))buf[--n]=0;
    for(size_t i=0;i<n;i++)buf[i]=(char)tolower((unsigned char)buf[i]);
    return strcmp(buf,"remarkable chiappa")==0 || strcmp(buf,"remarkable paper pro move")==0;
}
static uintptr_t unique_target(void) {
    FILE *f=fopen("/proc/self/maps","r"); char line[1024]; uintptr_t found=0; int count=0; if(!f)return 0;
    while(fgets(line,sizeof(line),f)) { unsigned long start,end; char perms[5]={0},path[768]={0};
        if(sscanf(line,"%lx-%lx %4s %*s %*s %*s %767s",&start,&end,perms,path)!=4)continue;
        if(perms[0]!='r'||perms[2]!='x'||strcmp(path,"/usr/bin/xochitl")!=0)continue;
        const uint8_t *p=(const uint8_t *)(uintptr_t)start; size_t size=(size_t)(end-start), plen=sizeof(TARGET_PATTERN);
        for(size_t i=0;i+plen<=size;i++)if(memcmp(p+i,TARGET_PATTERN,plen)==0){found=(uintptr_t)(p+i);count++;if(count>1){fclose(f);return 0;}}
    }
    fclose(f); return count==1?found:0;
}
static int setting_enabled(void) {
    FILE *f=fopen(CONFIG_PATH,"rb"); char line[512]; int section=0,master=0,snap=0,seen_master=0,seen_snap=0,invalid=0; if(!f)return 0;
    while(fgets(line,sizeof(line),f)){
        char *p=line,*key,*value,*end; while(isspace((unsigned char)*p))p++;
        if(*p=='['){end=strchr(p,']');if(end)end[1]=0;section=strcmp(p,"[RmtoolReadingEnhancements]")==0;continue;}
        if(!section||*p=='#'||*p==';')continue; key=p;value=strchr(p,'=');if(!value)continue;*value++=0;
        end=key+strlen(key);while(end>key&&isspace((unsigned char)end[-1]))*--end=0;while(isspace((unsigned char)*value))value++;
        end=value+strlen(value);while(end>value&&isspace((unsigned char)end[-1]))*--end=0;
        int truth=strcasecmp(value,"true")==0||strcmp(value,"1")==0;
        if(strcmp(key,"masterEnabled")==0){if(seen_master++)invalid=1;master=truth;}
        else if(strcmp(key,"hlSnapCjk")==0){if(seen_snap++)invalid=1;snap=truth;}
    }
    fclose(f);return !invalid&&seen_master==1&&seen_snap==1&&master&&snap;
}
static int is_cjk(long scene,int index) { long glyphs=0;uint16_t ch=0;if(index<0)return 0;memcpy(&glyphs,(void *)(scene+8),sizeof(glyphs));if(!glyphs)return 0;memcpy(&ch,(void *)(glyphs+(long)index*0x38+0x30),sizeof(ch));return (ch>=0x4e00&&ch<=0x9fff)||(ch>=0x3400&&ch<=0x4dbf)||(ch>=0xf900&&ch<=0xfaff)||(ch>=0x3000&&ch<=0x303f); }

static void far_jump(uint32_t out[5],uintptr_t target){out[0]=0xd2800010|(((target>>0)&0xffff)<<5);out[1]=0xf2a00010|(((target>>16)&0xffff)<<5);out[2]=0xf2c00010|(((target>>32)&0xffff)<<5);out[3]=0xf2e00010|(((target>>48)&0xffff)<<5);out[4]=0xd61f0200;}
typedef void (*ExpandFn)(long,void *); static ExpandFn original_expand;
static void handler(long scene,void *range){if(setting_enabled()&&range){uint64_t *v=range;int *sub=(int *)(uintptr_t)v[1];long count=(long)v[2];if(sub&&count>0&&count<100000&&is_cjk(scene,sub[0]))return;}if(original_expand)original_expand(scene,range);}
static int install(uintptr_t target){long ps=sysconf(_SC_PAGESIZE);if(ps<=0)ps=4096;uintptr_t page=target&~((uintptr_t)ps-1);size_t len=((target-page)+PATCH_LEN>(size_t)ps)?(size_t)ps*2:(size_t)ps;uint8_t *stub=mmap(NULL,PATCH_LEN+PATCH_LEN,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);if(stub==MAP_FAILED)return 0;memcpy(stub,(void *)target,PATCH_LEN);uint32_t jump[5];far_jump(jump,target+PATCH_LEN);memcpy(stub+PATCH_LEN,jump,PATCH_LEN);if(mprotect(stub,PATCH_LEN*2,PROT_READ|PROT_EXEC)!=0){munmap(stub,PATCH_LEN*2);return 0;}original_expand=(ExpandFn)stub;if(mprotect((void *)page,len,PROT_READ|PROT_WRITE|PROT_EXEC)!=0){original_expand=0;munmap(stub,PATCH_LEN*2);return 0;}far_jump(jump,(uintptr_t)handler);memcpy((void *)target,jump,PATCH_LEN);__builtin___clear_cache((char *)stub,(char *)stub+PATCH_LEN*2);__builtin___clear_cache((char *)target,(char *)target+PATCH_LEN);if(mprotect((void *)page,len,PROT_READ|PROT_EXEC)!=0){memcpy((void *)target,stub,PATCH_LEN);__builtin___clear_cache((char *)target,(char *)target+PATCH_LEN);mprotect((void *)page,len,PROT_READ|PROT_EXEC);original_expand=0;munmap(stub,PATCH_LEN*2);return 0;}return 1;}

char _xovi_shouldLoad(void){return machine_matches()&&file_text_equals("/etc/version",EXPECTED_FIRMWARE)&&xochitl_hash_matches()&&unique_target()!=0;}
void _xovi_construct(void){if(!_xovi_shouldLoad())return;uintptr_t target=unique_target();if(target)install(target);}

struct XoviMetadataEntry;
__attribute__((section(".xovi"))) const char *LINKTABLENAMES="\0";
__attribute__((section(".xovi"))) const void *LINKTABLEVALUES[]={(void *)0};
__attribute__((section(".xovi"))) const void *Environment=0;
__attribute__((section(".xovi"))) int EXTENSIONVERSION=65536;
__attribute__((section(".xovi_info"))) const char __XOVIMETADATANAMES[]="";
__attribute__((section(".xovi"))) const struct XoviMetadataEntry **METADATAVALUES[]={(void *)0,(void *)1};
void _xovi_depconstruct(void){}
