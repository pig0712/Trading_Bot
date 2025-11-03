#define NPY_NO_DEPRECATED_API NPY_1_7_API_VERSION
#include <Python.h>
#include <numpy/arrayobject.h>
#include <math.h>
#include <stdlib.h>

static inline double maxd(double a,double b){return a>b?a:b;}
static inline double clamp_nonneg(double x){return (x<0.0 || isnan(x))?0.0:x;}

// SMA
static void compute_sma(double* out, const double* x, npy_intp n, int w){
    if(w<=1){
        for(npy_intp i=0;i<n;i++) out[i]=x[i];
        return;
    }
    double s=0.0;
    for(npy_intp i=0;i<n;i++){
        s += x[i];
        if(i>=w) s -= x[i-w];
        out[i] = (i+1>=w)? s/(double)w : NAN;
    }
}

static PyObject* ma_cross_backtest(PyObject* self, PyObject* args, PyObject* kwargs){
    static char* kwlist[]={"prices","fast","slow","fee_rate","slip_bps","take_profit","stop_loss",NULL};
    PyObject* prices_obj=NULL;
    int fast=9, slow=21;
    double fee_rate=0.0004, slip_bps=1.0, tp=-1.0, sl=-1.0;

    if(!PyArg_ParseTupleAndKeywords(args, kwargs, "O|iidddd", kwlist,
        &prices_obj, &fast, &slow, &fee_rate, &slip_bps, &tp, &sl)){
        return NULL;
    }

    // 합리적 범위 방어
    if(fast<1) fast=1;
    if(slow<fast) slow=fast;
    if(fee_rate<0.0) fee_rate=0.0; if(fee_rate>0.05) fee_rate=0.05;
    if(slip_bps<0.0) slip_bps=0.0; if(slip_bps>1000.0) slip_bps=1000.0;

    PyArrayObject* arr=(PyArrayObject*)PyArray_FROM_OTF(prices_obj, NPY_FLOAT64, NPY_ARRAY_CARRAY);
    if(!arr){PyErr_SetString(PyExc_TypeError,"prices must be float64 1D"); return NULL;}
    if(PyArray_NDIM(arr)!=1){Py_DECREF(arr); PyErr_SetString(PyExc_ValueError,"prices must be 1D"); return NULL;}

    npy_intp n=PyArray_DIM(arr,0);
    if(n<(npy_intp)(slow+2)){Py_DECREF(arr); PyErr_SetString(PyExc_ValueError,"not enough data for slow MA"); return NULL;}
    double* px=(double*)PyArray_DATA(arr);

    // temp
    double* f=(double*)malloc(sizeof(double)*n);
    double* s=(double*)malloc(sizeof(double)*n);
    if(!f || !s){ if(f)free(f); if(s)free(s); Py_DECREF(arr); PyErr_NoMemory(); return NULL; }

    compute_sma(f,px,n,fast);
    compute_sma(s,px,n,slow);

    // 초기 상태
    const double initial_cash = 10000.0;   // (필요하면 v2에서 인자로 빼자)
    double cash = initial_cash;
    double pos  = 0.0;
    double entry = NAN;
    double peak = cash, maxdd=0.0;
    int trades=0, wins=0;

    // 수치 안정성 상수
    const double eps = 1e-12;
    const double lot_round = 1e-8; // 수량 라운딩(더스트 남겨 전액 0 방지)

    for(npy_intp i=1;i<n;i++){
        double price = px[i];
        if(!(price>0.0)) continue; // 0/NaN 가격 방어

        // TP/SL
        if(pos>0.0 && !isnan(entry)){
            double pnl = (price-entry)/entry;
            int hit = 0; const char* reason="";

            if(tp>0.0 && pnl>=tp){ hit=1; reason="tp"; }
            else if(sl>0.0 && pnl<=-sl){ hit=1; reason="sl"; }

            if(hit){
                double p_fill = price*(1.0 - slip_bps/10000.0);
                double proceeds = pos*p_fill;
                double fee = proceeds*fee_rate;
                cash += (proceeds - fee);
                if(p_fill>entry) wins++;
                trades++;
                pos=0.0; entry=NAN;
                cash = clamp_nonneg(cash);
            }
        }

        // 시그널
        double pf=f[i-1], ps=s[i-1], cf=f[i], cs=s[i];
        int long_entry = (!isnan(pf)&&!isnan(ps)&&!isnan(cf)&&!isnan(cs) && pf<=ps && cf>cs);
        int long_exit  = (!isnan(pf)&&!isnan(ps)&&!isnan(cf)&&!isnan(cs) && pf>=ps && cf<cs);

        if(long_entry && pos<=0.0){
            double p_fill = price*(1.0 + slip_bps/10000.0);
            double denom = p_fill*(1.0 + fee_rate);
            if(denom>eps){
                double budget = cash;
                // 라운딩으로 더스트 남김 → 현금이 정확히 0 되는 것 방지
                double qty = floor((budget/denom)/lot_round)*lot_round;
                if(qty>0.0){
                    double cost = qty*p_fill;
                    double fee  = cost*fee_rate;
                    double new_cash = budget - (cost + fee);
                    if(new_cash < 0.0 && fabs(new_cash) < 1e-6) new_cash = 0.0; // 미세 음수 클램프
                    if(new_cash >= -eps){
                        cash = clamp_nonneg(new_cash);
                        pos  = qty;
                        entry= p_fill;
                    }
                }
            }
        }else if(long_exit && pos>0.0){
            double p_fill = price*(1.0 - slip_bps/10000.0);
            double proceeds = pos*p_fill;
            double fee = proceeds*fee_rate;
            cash += (proceeds - fee);
            if(p_fill>entry) wins++;
            trades++;
            pos=0.0; entry=NAN;
            cash = clamp_nonneg(cash);
        }

        // MDD
        double equity = cash + pos*price;
        peak = maxd(peak, equity);
        if(peak>0.0){
            double dd=(peak-equity)/peak;
            if(dd>maxdd) maxdd=dd;
        }
    }

    // ✅ 마지막 캔들에서 미청산 포지션 강제 청산
    if(pos>0.0){
        double last = px[n-1];
        if(last>0.0){
            double p_fill = last*(1.0 - slip_bps/10000.0);
            double proceeds = pos*p_fill;
            double fee = proceeds*fee_rate;
            cash += (proceeds - fee);
            if(!isnan(entry) && p_fill>entry) wins++;
            trades++;
            pos=0.0; entry=NAN;
            cash = clamp_nonneg(cash);
        }
    }

    double final_equity = cash;  // 포지션은 위에서 정리
    if(!(final_equity>=0.0)) final_equity = 0.0;

    double total_return = (initial_cash>0.0)? (final_equity/initial_cash - 1.0) : 0.0;
    double win_rate = (trades>0)? ((double)wins/(double)trades) : 0.0;

    Py_DECREF(arr);
    free(f); free(s);

    PyObject* out=PyDict_New();
    PyDict_SetItemString(out,"final_equity", PyFloat_FromDouble(final_equity));
    PyDict_SetItemString(out,"total_return", PyFloat_FromDouble(total_return));
    PyDict_SetItemString(out,"max_drawdown", PyFloat_FromDouble(maxdd));
    PyDict_SetItemString(out,"n_trades", PyLong_FromLong(trades));
    PyDict_SetItemString(out,"win_rate", PyFloat_FromDouble(win_rate));
    return out;
}

static PyMethodDef Methods[]={
    {"ma_cross_backtest",(PyCFunction)ma_cross_backtest,METH_VARARGS|METH_KEYWORDS,"MA cross long-only backtest (C)"},
    {NULL,NULL,0,NULL}
};

static struct PyModuleDef Mod = {
    PyModuleDef_HEAD_INIT,"btcore","Backtest core (C)",-1,Methods,NULL,NULL,NULL,NULL
};

PyMODINIT_FUNC PyInit_btcore(void){
    import_array();
    return PyModule_Create(&Mod);
}
